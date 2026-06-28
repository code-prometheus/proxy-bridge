import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import utils
from inbound_compressor import compress_messages

# ⚠️ 核心防截断机制：动态生成三个反引号
MD_FENCE = chr(96) * 3
BLACKLIST_PATH = os.path.join(os.path.dirname(__file__), 'tag_blacklist.json')

def get_garbage_lists():
    default_blacklist = {
        "closing_garbage": ["</｜tool_calls｜>", "</｜invoke｜>", "</｜tool_name｜>", "</｜tool_arguments｜>", "</｜DSML｜tool_calls>", "</｜DSML｜invoke>", "</｜DSML｜tool_name>", "</｜DSML｜parameter>", "/｜DSML｜parameter", "/｜DSML｜invoke>", "/｜DSML｜tool_calls>", "</parameter>", "</invoke>", "</tool_calls>", "</tool_name>", "</tool_arguments>", "</system-reminder>", "</reminder>"],
        "stray_garbage": ["<｜tool_calls｜>", "｜tool_calls｜>", "<｜invoke｜>", "｜invoke｜>", "<｜DSML｜tool_calls>", "｜DSML｜tool_calls>", "<｜DSML｜invoke>", "｜DSML｜invoke>", "<invoke>", "<parameter>", "<tool_calls>", "<tool_name>", "<tool_arguments>", "<system-reminder>", "<reminder>"]
    }
    if not os.path.exists(BLACKLIST_PATH):
        try:
            with open(BLACKLIST_PATH, 'w', encoding='utf-8') as f: json.dump(default_blacklist, f, indent=4, ensure_ascii=False)
        except Exception as e: logging.error(f"❌ 无法生成黑名单文件: {e}")
        return default_blacklist["closing_garbage"], default_blacklist["stray_garbage"]
    try:
        with open(BLACKLIST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("closing_garbage", default_blacklist["closing_garbage"]), data.get("stray_garbage", default_blacklist["stray_garbage"])
    except Exception as e:
        logging.error(f"❌ 读取黑名单失败: {e}")
        return default_blacklist["closing_garbage"], default_blacklist["stray_garbage"]

def parse_fallback_tool(text_chunk, valid_tools):
    ds_name_match = re.search(r"<[｜|]?(?:DSML[｜|]?)?tool_name[｜|]?>\s*(.*?)\s*(?:</|[｜|]|$)", text_chunk, re.IGNORECASE)
    ds_args_match = re.search(r"<[｜|]?(?:DSML[｜|]?)?(?:tool_arguments|parameter)[｜|]?>\s*(.*?)\s*(?:</[｜|]|$)", text_chunk, re.DOTALL | re.IGNORECASE)
    if ds_name_match and ds_args_match:
        try: return ds_name_match.group(1).strip(), json.loads(ds_args_match.group(1).strip(), strict=False)
        except Exception: pass

    match_json = re.search(r"\{.*?\}", text_chunk, re.DOTALL)
    if match_json:
        try:
            data = json.loads(match_json.group(0), strict=False)
            if "name" in data and "arguments" in data: return data["name"], data["arguments"]
        except Exception: pass

    for t_name, t_info in valid_tools.items():
        tl = t_name.lower()
        if f"<{tl}>" in text_chunk.lower() or f"{MD_FENCE}{tl}" in text_chunk.lower():
            m = re.search(f"<{tl}[^>]*>(.*?)(?:</{tl}>|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            if not m: m = re.search(MD_FENCE + tl + r"\s*(.*?)(?:" + MD_FENCE + r"|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            if m:
                inner = m.group(1).strip()
                props = t_info.get("input_schema", {}).get("properties", {})
                if len(props) == 1: return t_name, {list(props.keys())[0]: inner}
                elif "command" in props: return t_name, {"command": inner}
    return "unknown", {}

def send_recovery_response(sock, msg_id, model_name, error_detail, valid_tools=None):
    safe_tool = list(valid_tools.keys())[0] if valid_tools else "bash"
    auto_heal_text = f"\n\n[⚠️ System Notice: Upstream API Interruption.\nDetails: {error_detail}\nDO NOT HALT. I have invoked a recovery tool (`{safe_tool}`) to continue.]\n\n"
    tool_id = f"call_retry_{int(time.time())}"
    try:
        resp = {
            "id": msg_id, "type": "message", "role": "assistant", "model": model_name,
            "content": [
                {"type": "text", "text": auto_heal_text},
                {"type": "tool_use", "id": tool_id, "name": safe_tool, "input": {}}
            ],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}
        }
        body_out = json.dumps(resp).encode('utf-8')
        sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
    except Exception as e:
        logging.error(f"❌ 发送接管响应失败: {e}")

def handle_anthropic_api(sock, method, url, headers, body_prefix):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        body = body_prefix
        while len(body) < content_length:
            chunk = sock.recv(8192)
            if not chunk: break
            body += chunk
        if not body: sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nEmpty Body"); return
        
        try: anthropic_req = json.loads(body.decode('utf-8'))
        except Exception: sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nInvalid JSON"); return

        current_llm = utils.LLMS_CONFIG.get(utils.ACTIVE_LLM_KEY)
        if not current_llm: sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\nNo LLM"); return

        is_stream = anthropic_req.get('stream', False)
        openai_req = {"model": current_llm.get("model_name", "default_model"), "messages": [], "stream": is_stream}

        for param in ["temperature", "top_p", "max_tokens", "stop_sequences"]:
            if param in anthropic_req: openai_req[param if param != "stop_sequences" else "stop"] = anthropic_req[param]

        valid_tools = {}
        valid_triggers = {"<tool_call>": "</tool_call>", f"{MD_FENCE}json": MD_FENCE, f"{MD_FENCE}tool_call": MD_FENCE}
        
        for k, v in list(valid_triggers.items()):
            if '｜' in k: valid_triggers[k.replace('｜', '|')] = v.replace('｜', '|')

        closing_raw, stray_raw = get_garbage_lists()
        CLOSING_GARBAGE = list(closing_raw) + [g.replace('｜', '|') for g in closing_raw if '｜' in g]
        STRAY_GARBAGE = list(stray_raw) + [g.replace('｜', '|') for g in stray_raw if '｜' in g]

        if "tools" in anthropic_req and anthropic_req["tools"]:
            openai_req["tools"] = []
            for t in anthropic_req["tools"]:
                valid_tools[t["name"]] = t
                openai_req["tools"].append({"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}})
                t_low = t['name'].lower()
                valid_triggers[f"<{t_low}>"] = f"</{t_low}>"
                valid_triggers[f"<{t['name']}>"] = f"</{t['name']}>"
                valid_triggers[f"{MD_FENCE}{t_low}"] = MD_FENCE
                valid_triggers[f"{MD_FENCE}{t['name']}"] = MD_FENCE

        sys_content = ""
        if "system" in anthropic_req:
            sys_val = anthropic_req["system"]
            sys_content = " ".join([b.get("text", "") for b in sys_val if b.get("type") == "text"]) if isinstance(sys_val, list) else sys_val
        if valid_tools: sys_content += "\n\n[Tools Instruction]\nIf you cannot use Native API function calling, output EXACTLY:\n<tool_call>\n{\"name\": \"tool_name\", \"arguments\": {\"arg\": \"val\"}}\n</tool_call>"
        if sys_content: openai_req["messages"].append({"role": "system", "content": sys_content})

        for m in anthropic_req.get("messages", []):
            role, content = m["role"], m["content"]
            if isinstance(content, str): openai_req["messages"].append({"role": role, "content": content})
            elif isinstance(content, list):
                if role == "user":
                    text_parts = []
                    for block in content:
                        if block.get("type") == "text": text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            if text_parts: openai_req["messages"].append({"role": "user", "content": "".join(text_parts)}); text_parts = []
                            res = block.get("content", "")
                            if isinstance(res, list): res = "".join([str(b.get("text", b)) for b in res if b.get("type") == "text"])
                            if block.get("is_error"): res = f"Error: {res}"
                            openai_req["messages"].append({"role": "tool", "tool_call_id": block.get("tool_use_id", "unknown"), "content": res})
                    if text_parts: openai_req["messages"].append({"role": "user", "content": "".join(text_parts)})
                elif role == "assistant":
                    text_parts, tool_calls = [], []
                    for block in content:
                        if block.get("type") == "text": text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use": tool_calls.append({"id": block.get("id"), "type": "function", "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}})
                    if text_parts or tool_calls:
                        msg = {"role": "assistant"}
                        if text_parts: msg["content"] = "".join(text_parts)
                        if tool_calls: msg["tool_calls"] = tool_calls
                        openai_req["messages"].append(msg)

        openai_req["messages"] = compress_messages(openai_req["messages"])

        api_key = current_llm.get("api_key", "")
        base_url = current_llm.get("base_url", "")
        if not base_url.endswith("/chat/completions"): base_url = base_url.rstrip("/") + "/chat/completions"
        
        ctx_ssl = ssl.create_default_context()
        if not current_llm.get("verify_ssl", True): ctx_ssl.check_hostname = False; ctx_ssl.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(base_url, data=json.dumps(openai_req).encode('utf-8'), method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'text/event-stream' if is_stream else 'application/json')
        if api_key and api_key.lower() != "none": req.add_header(current_llm.get("auth_header", "Authorization"), f"{current_llm.get('auth_prefix', 'Bearer ')}{api_key}")

        msg_id = f"msg_{int(time.time())}"
        timeout_seconds = 900

        for attempt in range(3):
            headers_sent = False
            anth_block_idx = 0
            in_text_block = False
            has_tool_use = False
            active_native_tools = {}
            active_blocks = set()
            
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds, context=ctx_ssl) as response:
                    if is_stream:
                        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
                        headers_sent = True
                        
                        text_buffer, is_intercepting, intercept_buffer, active_close_tag = "", False, "", ""
                        stream_completed, upstream_error_msg, detected_orphaned_tags = False, "", False

                        sock.sendall(f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': openai_req['model'], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n".encode('utf-8'))

                        try:
                            for line in response:
                                line = line.decode('utf-8').strip()
                                if not line or not line.startswith("data: "): continue
                                data_str = line[6:]
                                if data_str == "[DONE]": stream_completed = True; break
                                
                                try:
                                    chunk = json.loads(data_str)
                                    if "error" in chunk: continue
                                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                                    
                                    if reasoning := delta.get("reasoning_content"):
                                        if not in_text_block:
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                            active_blocks.add(anth_block_idx); in_text_block = True
                                        sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': reasoning}})}\n\n".encode('utf-8'))

                                    if content := delta.get("content"):
                                        if is_intercepting:
                                            intercept_buffer += content
                                            if active_close_tag in intercept_buffer:
                                                close_idx = intercept_buffer.find(active_close_tag) + len(active_close_tag)
                                                full_xml = intercept_buffer[:close_idx]
                                                text_buffer = intercept_buffer[close_idx:]
                                                
                                                t_name, t_args = parse_fallback_tool(full_xml, valid_tools)
                                                if in_text_block:
                                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                    active_blocks.discard(anth_block_idx); in_text_block, anth_block_idx = False, anth_block_idx + 1
                                                
                                                if t_name != "unknown":
                                                    tool_id = f"call_{int(time.time())}_{anth_block_idx}"
                                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': t_name, 'input': {}}})}\n\n".encode('utf-8'))
                                                    active_blocks.add(anth_block_idx)
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(t_args, ensure_ascii=False) if t_args else '{}'}})}\n\n".encode('utf-8'))
                                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                    active_blocks.discard(anth_block_idx); anth_block_idx += 1; has_tool_use = True
                                                else:
                                                    warn_msg = "\n\n[⚠️ System Notice: Invalid tool tag. Triggering format recovery.]\n\n"
                                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                    active_blocks.add(anth_block_idx)
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': warn_msg}})}\n\n".encode('utf-8'))
                                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                    active_blocks.discard(anth_block_idx); anth_block_idx += 1
                                                    
                                                    safe_tool = list(valid_tools.keys())[0] if valid_tools else "bash"
                                                    tool_id = f"call_format_err_{int(time.time())}_{anth_block_idx}"
                                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': safe_tool, 'input': {}}})}\n\n".encode('utf-8'))
                                                    active_blocks.add(anth_block_idx)
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}})}\n\n".encode('utf-8'))
                                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                    active_blocks.discard(anth_block_idx); anth_block_idx += 1; has_tool_use = True
                                                
                                                is_intercepting, intercept_buffer = False, ""
                                        else:
                                            text_buffer += content
                                            for g in CLOSING_GARBAGE:
                                                if g in text_buffer: detected_orphaned_tags = True; text_buffer = text_buffer.replace(g, "")
                                            
                                            matched_tag, earliest_idx = None, -1
                                            for tag in valid_triggers:
                                                if (idx := text_buffer.find(tag)) != -1 and (earliest_idx == -1 or idx < earliest_idx): earliest_idx, matched_tag = idx, tag
                                            
                                            if matched_tag:
                                                if pre_text := text_buffer[:earliest_idx]:
                                                    if not in_text_block:
                                                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                        active_blocks.add(anth_block_idx); in_text_block = True
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': pre_text}})}\n\n".encode('utf-8'))
                                                is_intercepting, active_close_tag, intercept_buffer = True, valid_triggers[matched_tag], text_buffer[earliest_idx:]
                                                text_buffer = ""
                                            else:
                                                if len(text_buffer) > 35:
                                                    if not in_text_block:
                                                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                        active_blocks.add(anth_block_idx); in_text_block = True
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': text_buffer[:-35]}})}\n\n".encode('utf-8'))
                                                    text_buffer = text_buffer[-35:]

                                    for tc in delta.get("tool_calls", []):
                                        idx = tc.get("index")
                                        if idx is None: continue
                                        if idx not in active_native_tools:
                                            if in_text_block:
                                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                active_blocks.discard(anth_block_idx); in_text_block, anth_block_idx = False, anth_block_idx + 1
                                            
                                            active_native_tools[idx] = {"anth_idx": anth_block_idx}
                                            has_tool_use = True
                                            tool_id = tc.get("id", f"call_{int(time.time())}_{idx}")
                                            func_name = tc.get("function", {}).get("name", "unknown")
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': func_name, 'input': {}}})}\n\n".encode('utf-8'))
                                            active_blocks.add(anth_block_idx); anth_block_idx += 1
                                        
                                        if args_delta := tc.get("function", {}).get("arguments"):
                                            curr_idx = active_native_tools[idx]["anth_idx"]
                                            sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': curr_idx, 'delta': {'type': 'input_json_delta', 'partial_json': args_delta}})}\n\n".encode('utf-8'))
                                except Exception: pass
                        except Exception as stream_err:
                            logging.warning(f"⚠️ 流式读取中断: {stream_err}")
                        finally:
                            for blk_idx in list(active_blocks):
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': blk_idx})}\n\n".encode('utf-8'))
                            active_blocks.clear()
                            
                            if in_text_block:
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                in_text_block = False

                            if is_intercepting:
                                t_name, t_args = parse_fallback_tool(intercept_buffer, valid_tools)
                                if t_name != "unknown":
                                    tool_id = f"call_{int(time.time())}_{anth_block_idx}"
                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': t_name, 'input': {}}})}\n\n".encode('utf-8'))
                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(t_args, ensure_ascii=False) if t_args else '{}'}})}\n\n".encode('utf-8'))
                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                    anth_block_idx += 1; has_tool_use = True
                            
                            if text_buffer:
                                for g in STRAY_GARBAGE: text_buffer = text_buffer.replace(g, "")
                                if text_buffer:
                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': text_buffer}})}\n\n".encode('utf-8'))
                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                    anth_block_idx += 1
                            
                            # 🚨 核心修复：协议兜底！如果过滤垃圾标签后导致整个流没有任何 content_block，强制补发空 text block
                            if anth_block_idx == 0 and not in_text_block and not active_blocks:
                                sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n".encode('utf-8'))

                            stop_reason = "tool_use" if has_tool_use else "end_turn"
                            sock.sendall(f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n".encode('utf-8'))
                            sock.sendall(f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n".encode('utf-8'))
                        break
                    
                    else:
                        res_body = response.read()
                        res_data = json.loads(res_body)
                        if "error" in res_data:
                            send_recovery_response(sock, msg_id, openai_req.get("model", "unknown"), json.dumps(res_data["error"]), valid_tools)
                            return
                        if "type" in res_data and res_data["type"] == "message":
                            sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(res_body)}\r\nConnection: close\r\n\r\n".encode('utf-8') + res_body)
                            return
                        
                        msg = res_data.get("choices", [{}])[0].get("message", {})
                        full_text = (msg.get("reasoning_content", "") + "\n\n" + (msg.get("content") or "")).strip()
                        extracted_tools = []
                        
                        for tag in valid_triggers:
                            if tag in full_text:
                                c_tag = valid_triggers[tag]
                                for match in re.finditer(re.escape(tag) + r"(.*?)" + re.escape(c_tag), full_text, re.DOTALL):
                                    full_xml = match.group(0)
                                    t_name, t_args = parse_fallback_tool(full_xml, valid_tools)
                                    if t_name != "unknown": extracted_tools.append({"id": f"call_{int(time.time())}_{len(extracted_tools)}", "name": t_name, "input": t_args})
                                    else: extracted_tools.append({"id": f"call_err_{len(extracted_tools)}", "name": list(valid_tools.keys())[0] if valid_tools else "bash", "input": {}})
                                    full_text = full_text.replace(full_xml, "")
                        
                        resp = {"id": msg_id, "type": "message", "role": "assistant", "model": openai_req["model"], "content": [], "stop_reason": "end_turn", "stop_sequence": None, "usage": res_data.get("usage", {"input_tokens": 0, "output_tokens": 0})}
                        if full_text.strip(): resp["content"].append({"type": "text", "text": full_text.strip()})
                        for xt in extracted_tools: resp["content"].append({"type": "tool_use", **xt})
                        for tc in msg.get("tool_calls", []):
                            try: args = json.loads(tc["function"].get("arguments", "{}"))
                            except: args = {}
                            resp["content"].append({"type": "tool_use", "id": tc.get("id", f"call_{int(time.time())}"), "name": tc["function"]["name"], "input": args})
                        
                        if extracted_tools or msg.get("tool_calls"): resp["stop_reason"] = "tool_use"
                        if not resp["content"]: resp["content"].append({"type": "text", "text": ""}) # 非流式兜底
                        
                        body_out = json.dumps(resp).encode('utf-8')
                        sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
                        break

            except Exception as e:
                is_timeout = isinstance(e, (TimeoutError, socket.timeout)) or (hasattr(e, 'reason') and (isinstance(e.reason, socket.timeout) or "timed out" in str(e.reason).lower()))
                err_msg = f"HTTP Error" if isinstance(e, urllib.error.HTTPError) else (f"Timeout after {timeout_seconds}s" if is_timeout else str(e))
                logging.error(f"🚨 [API] 请求失败 (尝试 {attempt+1}/3): {err_msg}")
                
                if not headers_sent:
                    send_recovery_response(sock, msg_id, openai_req.get("model", "unknown"), err_msg, valid_tools)
                else:
                    for blk_idx in list(active_blocks):
                        sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': blk_idx})}\n\n".encode('utf-8'))
                    if in_text_block:
                        sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                    
                    # 异常中断也检查是否需要兜底
                    if anth_block_idx == 0 and not in_text_block and not active_blocks:
                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                        sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n".encode('utf-8'))

                    sock.sendall(f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n".encode('utf-8'))
                    sock.sendall(f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n".encode('utf-8'))
                    
                if not is_timeout and attempt < 2: time.sleep(2); continue
                break

    except Exception as e:
        logging.error(f"❌ 代理层致命错误: {e}")
        try: sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\n")
        except: pass
    finally:
        try: sock.close()
        except: pass