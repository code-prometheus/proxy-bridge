# Anthropic 协议代理核心 (严格对标 CC Switch 极简原生映射与极速流式版)
# 核心职责：万能出站协议转换，无论下游是 OpenAI 原生工具，
# 还是纯文本降级格式的工具，统统极速归一化为 Anthropic 标准 tool_use 事件返回！
# 约束：抛弃一切网页 Wrapper 的土味兼容，回归标准 OpenAI/Anthropic 协议规范映射！
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request

import utils
from inbound_compressor import compress_messages

# ⚠️ 核心防截断机制：动态生成三个反引号，避免代码块被前端界面意外暴力截断
MD_FENCE = chr(96) * 3

def parse_fallback_tool(text_chunk, valid_tools):
    """
    极速后备提取引擎：从大模型的纯文本中抽取工具意图。
    利用结构化提取替换脆弱的逐字符正则修正，保障最高运行效率。
    """
    tool_name = "unknown"
    args_dict = {}

    # 1. 尝试提取被包裹的标准 JSON 对象
    match_json = re.search(r"\{.*?\}", text_chunk, re.DOTALL)
    if match_json:
        try:
            data = json.loads(match_json.group(0), strict=False)
            if "name" in data and "arguments" in data:
                return data["name"], data["arguments"]
        except:
            pass

    # 2. 尝试暴力提取裸标签 (如 <bash>ls</bash> 或 Markdown 语法)
    for t_name, t_info in valid_tools.items():
        tl = t_name.lower()
        if f"<{tl}>" in text_chunk.lower() or f"{MD_FENCE}{tl}" in text_chunk.lower():
            # 宽容匹配 XML 闭合或 Markdown 闭合
            m = re.search(f"<{tl}[^>]*>(.*?)(?:</{tl}>|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            if not m:
                m = re.search(MD_FENCE + tl + r"\s*(.*?)(?:" + MD_FENCE + r"|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            
            if m:
                inner_content = m.group(1).strip()
                props = t_info.get("input_schema", {}).get("properties", {})
                if len(props) == 1:
                    return t_name, {list(props.keys())[0]: inner_content}
                elif "command" in props:
                    return t_name, {"command": inner_content}

    return "unknown", {}

def handle_anthropic_api(sock, method, url, headers, body_prefix):
    try:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        body = body_prefix
        while len(body) < content_length:
            chunk = sock.recv(8192)
            if not chunk: break
            body += chunk
        if not body:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nEmpty Body")
            return

        try:
            body_str = body.decode('utf-8')
        except:
            body_str = body.decode('gbk', 'ignore')

        try:
            anthropic_req = json.loads(body_str)
        except:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nInvalid JSON Format")
            return

        current_llm = utils.LLMS_CONFIG.get(utils.ACTIVE_LLM_KEY)
        if not current_llm:
            sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\nNo LLM config")
            return

        is_stream = anthropic_req.get('stream', False)
        
        # ================= 入站请求转换 (Anthropic -> 通用 OpenAI) =================
        openai_req = {
            "model": current_llm.get("model_name", "default_model"),
            "messages": [],
            "stream": is_stream
        }

        # 核心参数透传
        for param in ["temperature", "top_p", "max_tokens", "stop_sequences"]:
            if param in anthropic_req:
                openai_req[param if param != "stop_sequences" else "stop"] = anthropic_req[param]

        valid_tools = {}
        valid_triggers = {
            "<tool_call>": "</tool_call>",
            f"{MD_FENCE}json": MD_FENCE,
            f"{MD_FENCE}tool_call": MD_FENCE
        }
        
        # CC Switch 规范：原生态 Tool Mapping
        if "tools" in anthropic_req and len(anthropic_req["tools"]) > 0:
            openai_req["tools"] = []
            for t in anthropic_req["tools"]:
                valid_tools[t["name"]] = t
                openai_req["tools"].append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {})
                    }
                })
                # 动态构建文本降级拦截字典
                t_low = t['name'].lower()
                valid_triggers[f"<{t_low}>"] = f"</{t_low}>"
                valid_triggers[f"<{t['name']}>"] = f"</{t['name']}>"
                valid_triggers[f"{MD_FENCE}{t_low}"] = MD_FENCE
                valid_triggers[f"{MD_FENCE}{t['name']}"] = MD_FENCE

        # 提取系统提示词
        sys_content = ""
        if "system" in anthropic_req:
            sys_val = anthropic_req["system"]
            sys_content = "".join([b.get("text", "") for b in sys_val if b.get("type") == "text"]) if isinstance(sys_val, list) else sys_val
            
        if valid_tools:
            # 优雅降级：仅添加少量兜底指示，供不支持原生 Tool Call 的模型参考
            sys_content += f"\n\n[Tools Instruction]\nYou have tools. If you cannot use Native API function calling, output EXACTLY:\n<tool_call>\n{{\"name\": \"tool_name\", \"arguments\": {{\"arg\": \"val\"}}}}\n</tool_call>"

        if sys_content:
            openai_req["messages"].append({"role": "system", "content": sys_content})

        # ================= CC Switch 规范：双向角色与历史转换 =================
        for m in anthropic_req.get("messages", []):
            role = m["role"]
            content = m["content"]
            
            if isinstance(content, str):
                openai_req["messages"].append({"role": role, "content": content})
            elif isinstance(content, list):
                if role == "user":
                    text_parts = []
                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            if text_parts:
                                openai_req["messages"].append({"role": "user", "content": "".join(text_parts)})
                                text_parts = []
                            res_content = block.get("content", "")
                            if isinstance(res_content, list):
                                res_content = "".join([str(b.get("text", b)) for b in res_content if b.get("type") == "text"])
                            if block.get("is_error"): res_content = f"Error: {res_content}"
                            openai_req["messages"].append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", "unknown"),
                                "content": res_content
                            })
                    if text_parts:
                        openai_req["messages"].append({"role": "user", "content": "".join(text_parts)})
                elif role == "assistant":
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                                }
                            })
                    ast_msg = {"role": "assistant"}
                    if text_parts: ast_msg["content"] = "".join(text_parts)
                    if tool_calls: ast_msg["tool_calls"] = tool_calls
                    if text_parts or tool_calls:
                        openai_req["messages"].append(ast_msg)

        # 调用解耦入站压缩模块
        openai_req["messages"] = compress_messages(openai_req["messages"])

        api_key = current_llm.get("api_key", "")
        base_url = current_llm.get("base_url", "")
        if not base_url.endswith("/chat/completions"): base_url = base_url.rstrip("/") + "/chat/completions"

        ctx = ssl.create_default_context()
        if not current_llm.get("verify_ssl", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        logging.info(f"🤖 [API] [{utils.ACTIVE_LLM_KEY}] 代理映射请求已装配完成，即将直发下游...")

        max_retries = 3
        msg_id = f"msg_{int(time.time())}"

        for attempt in range(max_retries):
            req_body = json.dumps(openai_req, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(base_url, data=req_body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Accept', 'text/event-stream' if is_stream else 'application/json')
            req.add_header('User-Agent', 'CC-Switch/Proxy')
            if api_key and api_key.lower() != "none": 
                req.add_header(current_llm.get("auth_header", "Authorization"), f"{current_llm.get('auth_prefix', 'Bearer ')}{api_key}")

            try:
                with urllib.request.urlopen(req, timeout=600, context=ctx) as response:
                    
                    if is_stream:
                        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
                        
                        anth_block_idx = 0
                        in_text_block = False
                        
                        # 滑动窗口状态机：极速文本流拦截器
                        text_buffer = ""
                        is_intercepting = False
                        intercept_buffer = ""
                        active_close_tag = ""
                        
                        active_native_tools = {}
                        has_tool_use = False
                        has_generated_any = False

                        # 确保发送最初的流式响应头
                        sock.sendall(f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': openai_req['model'], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n".encode('utf-8'))

                        for line in response:
                            line = line.decode('utf-8').strip()
                            if not line: continue
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]": break
                                
                                try:
                                    chunk = json.loads(data_str)
                                    choices = chunk.get("choices", [])
                                    if not choices: continue
                                    delta = choices[0].get("delta", {})

                                    # ====== A. 极速纯文本与思维链直通机制 ======
                                    reasoning = delta.get("reasoning_content", "")
                                    content = delta.get("content", "")
                                    
                                    # 独立且极速放行推理块
                                    if reasoning:
                                        has_generated_any = True
                                        if not in_text_block:
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                            in_text_block = True
                                        sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': reasoning}})}\n\n".encode('utf-8'))

                                    # Content 的状态机截获引擎
                                    if content:
                                        has_generated_any = True
                                        if is_intercepting:
                                            intercept_buffer += content
                                            if active_close_tag in intercept_buffer:
                                                # 匹配到闭合标签，立刻转化
                                                close_idx = intercept_buffer.find(active_close_tag) + len(active_close_tag)
                                                full_xml = intercept_buffer[:close_idx]
                                                text_buffer = intercept_buffer[close_idx:]
                                                
                                                t_name, t_args = parse_fallback_tool(full_xml, valid_tools)
                                                if t_name != "unknown":
                                                    if in_text_block:
                                                        sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                        in_text_block = False
                                                        anth_block_idx += 1
                                                    
                                                    tool_id = f"call_{int(time.time())}_{anth_block_idx}"
                                                    sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': t_name, 'input': {}}})}\n\n".encode('utf-8'))
                                                    if t_args:
                                                        sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(t_args, ensure_ascii=False)}})}\n\n".encode('utf-8'))
                                                    sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                                    anth_block_idx += 1
                                                    has_tool_use = True
                                                else:
                                                    # 解析失败作为普通文本发还
                                                    if not in_text_block:
                                                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                        in_text_block = True
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': full_xml}})}\n\n".encode('utf-8'))
                                                    
                                                is_intercepting = False
                                                intercept_buffer = ""
                                        else:
                                            text_buffer += content
                                            matched_tag = None
                                            earliest_idx = -1
                                            
                                            # 探测是否有潜在的触发标签开始
                                            for tag in valid_triggers.keys():
                                                idx = text_buffer.find(tag)
                                                if idx != -1 and (earliest_idx == -1 or idx < earliest_idx):
                                                    earliest_idx, matched_tag = idx, tag
                                            
                                            if matched_tag:
                                                # 把标签之前的安全文本急速刷新给终端
                                                pre_text = text_buffer[:earliest_idx]
                                                if pre_text:
                                                    if not in_text_block:
                                                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                        in_text_block = True
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': pre_text}})}\n\n".encode('utf-8'))
                                                
                                                is_intercepting = True
                                                active_close_tag = valid_triggers[matched_tag]
                                                intercept_buffer = text_buffer[earliest_idx:]
                                                text_buffer = ""
                                            else:
                                                # 【极速刷新机制】只留下最后 15 个字符当做滑动窗口，前面的瞬间全部发出去响应给终端
                                                flush_len = max(0, len(text_buffer) - 15)
                                                if flush_len > 0:
                                                    if not in_text_block:
                                                        sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                                        in_text_block = True
                                                    sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': text_buffer[:flush_len]}})}\n\n".encode('utf-8'))
                                                    text_buffer = text_buffer[flush_len:]

                                    # ====== B. 原生 OpenAI tool_calls 严格映射 ======
                                    tool_calls = delta.get("tool_calls", [])
                                    if tool_calls and in_text_block:
                                        sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                        in_text_block = False
                                        anth_block_idx += 1
                                        
                                    for tc in tool_calls:
                                        has_generated_any = True
                                        idx = tc.get("index")
                                        if idx is None: continue
                                        
                                        if idx not in active_native_tools:
                                            active_native_tools[idx] = anth_block_idx
                                            has_tool_use = True
                                            
                                            tool_id = tc.get("id", f"call_{int(time.time())}_{idx}")
                                            func_name = tc.get("function", {}).get("name", "unknown")
                                            
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': func_name, 'input': {}}})}\n\n".encode('utf-8'))
                                            anth_block_idx += 1
                                            
                                        args_delta = tc.get("function", {}).get("arguments", "")
                                        if args_delta:
                                            curr_idx = active_native_tools[idx]
                                            sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': curr_idx, 'delta': {'type': 'input_json_delta', 'partial_json': args_delta}})}\n\n".encode('utf-8'))

                                except Exception:
                                    pass

                        # ====== 流式传输善后处理 ======
                        if is_intercepting:
                            # 处理 API 强行截断闭合标签的情景
                            t_name, t_args = parse_fallback_tool(intercept_buffer, valid_tools)
                            if t_name != "unknown":
                                tool_id = f"call_{int(time.time())}_{anth_block_idx}"
                                sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': t_name, 'input': {}}})}\n\n".encode('utf-8'))
                                sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(t_args, ensure_ascii=False)}})}\n\n".encode('utf-8'))
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))
                                anth_block_idx += 1
                                has_tool_use = True
                            else:
                                text_buffer = intercept_buffer + text_buffer
                        
                        # 吐出所有残余文本
                        if text_buffer:
                            if not in_text_block:
                                sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                in_text_block = True
                            sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': text_buffer}})}\n\n".encode('utf-8'))
                            
                        # 安全防暴毙检查
                        if not has_generated_any:
                            logging.error("🚨 下游无端断流！为防止死循环，抛出系统警告接管。")
                            if not in_text_block:
                                sock.sendall(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anth_block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode('utf-8'))
                                in_text_block = True
                            sock.sendall(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anth_block_idx, 'delta': {'type': 'text_delta', 'text': f'\\n\\n[⚠️ Proxy System: 请求瞬间中断。原因可能为 API 免费额度耗尽或单次上下文体积过大。请尝试使用 `/clear` 或检查您的代理/节点配置！]\\n\\n'}})}\n\n".encode('utf-8'))

                        if in_text_block:
                            sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anth_block_idx})}\n\n".encode('utf-8'))

                        for t_idx in active_native_tools.values():
                            sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': t_idx})}\n\n".encode('utf-8'))

                        stop_reason = "tool_use" if has_tool_use else "end_turn"
                        sock.sendall(f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n".encode('utf-8'))
                        sock.sendall(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
                        break
                    
                    else:
                        # ================= 非流式请求双向转换兜底 =================
                        res_body = response.read()
                        res_data = json.loads(res_body)
                        
                        if "error" in res_data:
                            err_msg = json.dumps(res_data).encode('utf-8')
                            sock.sendall(f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(err_msg)}\r\n\r\n".encode('utf-8') + err_msg)
                            return
                            
                        if "type" in res_data and res_data["type"] == "message":
                            sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(res_body)}\r\nConnection: close\r\n\r\n".encode('utf-8') + res_body)
                            break
                            
                        msg = res_data.get("choices", [{}])[0].get("message", {})
                        anthropic_resp = {
                            "id": msg_id, "type": "message", "role": "assistant",
                            "model": openai_req["model"], "content": [], "stop_reason": "end_turn",
                            "stop_sequence": None, "usage": res_data.get("usage", {"input_tokens": 0, "output_tokens": 0})
                        }
                        
                        full_text = (msg.get("reasoning_content", "") + "\n\n" + (msg.get("content") or "")).strip()
                        
                        extracted_tools = []
                        for tag in valid_triggers.keys():
                            if tag in full_text:
                                c_tag = valid_triggers[tag]
                                for match in re.finditer(re.escape(tag) + r"(.*?)" + re.escape(c_tag), full_text, re.DOTALL):
                                    full_xml = match.group(0)
                                    t_name, t_args = parse_fallback_tool(full_xml, valid_tools)
                                    if t_name != "unknown":
                                        extracted_tools.append({"id": f"call_{int(time.time())}_{len(extracted_tools)}", "name": t_name, "input": t_args})
                                        full_text = full_text.replace(full_xml, "")
                        
                        clean_text = full_text.strip()
                        if clean_text: anthropic_resp["content"].append({"type": "text", "text": clean_text})
                        
                        for xt in extracted_tools:
                            anthropic_resp["content"].append({"type": "tool_use", "id": xt["id"], "name": xt["name"], "input": xt["input"]})
                            
                        for tc in msg.get("tool_calls", []):
                            func = tc.get("function", {})
                            try: args = json.loads(func.get("arguments", "{}"))
                            except: args = {}
                            anthropic_resp["content"].append({"type": "tool_use", "id": tc.get("id", f"call_{int(time.time())}"), "name": func.get("name"), "input": args})
                            
                        if extracted_tools or msg.get("tool_calls"): anthropic_resp["stop_reason"] = "tool_use"
                        if not anthropic_resp["content"]: anthropic_resp["content"].append({"type": "text", "text": ""})
                        
                        body_out = json.dumps(anthropic_resp).encode('utf-8')
                        sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
                        break

            except urllib.error.HTTPError as e:
                res_body = e.read()
                logging.error(f"❌ [API] HTTP 错误 {e.code}: {e.reason}")
                res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\nContent-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                try: sock.sendall(res_head_str.encode('utf-8') + res_body)
                except: pass
                break
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\nConnection Failed".encode('utf-8'))
                break
            except Exception as outer_e:
                logging.error(f"❌ [API] 代理内部异常: {outer_e}")
                break

    except Exception:
        try: sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\n")
        except: pass
    finally:
        try: sock.close()
        except: pass