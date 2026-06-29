import json
import logging
import os
import re
import socket
import ssl
import time
import uuid
import urllib.error
import urllib.request
import utils
from inbound_compressor import compress_messages

# ==========================================
# 模块 1: 配置、黑名单与基础工具
# ==========================================
MD_FENCE = chr(96) * 3
BLACKLIST_PATH = os.path.join(os.path.dirname(__file__), 'tag_blacklist.json')

def gen_tool_id():
    """生成符合 Anthropic 严格规范的 tool_use ID (必须以 toolu_ 开头)"""
    return f"toolu_{uuid.uuid4().hex[:24]}"

def is_task_completed(text, stop_reason):
    """🚨 核心新增：判断 LLM 输出是否表明任务已完成，避免误触发防停工机制"""
    if stop_reason == 'length':
        return False  # 长度截断肯定没完成，必须恢复
    
    if not text:
        return False
        
    # 完成标志关键词（涵盖中英文、Emoji及常见的收尾客套话）
    completion_keywords = [
        "全部就绪", "任务完成", "已完成", "工作结束", "所有任务均已完成", 
        "没有更多需要", "无需进一步", "准备就绪", "成功完成", "执行完毕",
        "All tasks completed", "Task finished", "Done", "Ready", "Completed",
        "✅", "🎉", "✨", "需要做什么调整吗", "还有什么我可以", "请告诉我下一步"
    ]
    
    for kw in completion_keywords:
        if kw in text:
            return True
    return False

def get_garbage_lists():
    default_blacklist = {
        "closing_garbage": ["</｜tool_calls｜>", "</｜invoke｜>", "</｜tool_name｜>", "</｜tool_arguments｜>", "</｜DSML｜tool_calls>", "</｜DSML｜invoke>", "</｜DSML｜tool_name>", "</｜DSML｜parameter>", "/｜DSML｜parameter", "/｜DSML｜invoke>", "/｜DSML｜tool_calls>", "</parameter>", "</invoke>", "</tool_calls>", "</tool_name>", "</tool_arguments>", "</system-reminder>", "</reminder>"],
        "stray_garbage": ["<｜tool_calls｜>", "｜tool_calls｜>", "<｜invoke｜>", "｜invoke｜>", "<｜DSML｜tool_calls>", "｜DSML｜tool_calls>", "<｜DSML｜invoke>", "｜DSML｜invoke>", "<invoke>", "<parameter>", "<tool_calls>", "<tool_name>", "<tool_arguments>", "<system-reminder>", "<reminder>"]
    }
    if not os.path.exists(BLACKLIST_PATH):
        try:
            with open(BLACKLIST_PATH, 'w', encoding='utf-8') as f: 
                json.dump(default_blacklist, f, indent=4, ensure_ascii=False)
        except Exception as e: 
            logging.error(f"❌ 无法生成黑名单文件: {e}")
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
        try: 
            return ds_name_match.group(1).strip(), json.loads(ds_args_match.group(1).strip(), strict=False)
        except Exception: 
            pass
            
    match_json = re.search(r"\{.*?\}", text_chunk, re.DOTALL)
    if match_json:
        try:
            data = json.loads(match_json.group(0), strict=False)
            if "name" in data and "arguments" in data: 
                return data["name"], data["arguments"]
        except Exception: 
            pass

    for t_name, t_info in valid_tools.items():
        tl = t_name.lower()
        if f"<{tl}>" in text_chunk.lower() or f"{MD_FENCE}{tl}" in text_chunk.lower():
            m = re.search(f"<{tl}[^>]*>(.*?)(?:</{tl}>|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            if not m: 
                m = re.search(MD_FENCE + tl + r"\s*(.*?)(?:" + MD_FENCE + r"|$)", text_chunk, re.IGNORECASE | re.DOTALL)
            if m:
                inner = m.group(1).strip()
                props = t_info.get("input_schema", {}).get("properties", {})
                if len(props) == 1: 
                    return t_name, {list(props.keys())[0]: inner}
                elif "command" in props: 
                    return t_name, {"command": inner}
    return "unknown", {}

# ==========================================
# 模块 2: SSE 流式状态机 & 核心防停工机制
# ==========================================
class StreamContext:
    """严格管理 Anthropic SSE 协议，并包含 bash+echo 自动恢复机制"""
    def __init__(self, sock, msg_id, model_name, is_agent_mode):
        self.sock = sock
        self.msg_id = msg_id
        self.model_name = model_name
        self.is_agent_mode = is_agent_mode
        self.block_idx = 0
        self.text_open = False
        self.tool_open = False
        self.has_tool_use = False
        self.generated_text = ""  # 🚨 新增：记录生成的文本用于完成判断
        self._send_event('message_start', {
            'type': 'message_start', 
            'message': {
                'id': msg_id, 'type': 'message', 'role': 'assistant', 
                'content': [], 'model': model_name, 'stop_reason': None, 
                'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}
            }
        })

    def _send_event(self, event_type, data):
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode('utf-8')
        self.sock.sendall(payload)

    def ensure_text_open(self):
        if self.tool_open: 
            self.close_tool()
        if not self.text_open:
            self._send_event('content_block_start', {
                'type': 'content_block_start', 'index': self.block_idx, 
                'content_block': {'type': 'text', 'text': ''}
            })
            self.text_open = True

    def send_text_delta(self, text):
        if not text: 
            return
        self.generated_text += text  # 🚨 累加文本用于后续判断
        self.ensure_text_open()
        self._send_event('content_block_delta', {
            'type': 'content_block_delta', 'index': self.block_idx, 
            'delta': {'type': 'text_delta', 'text': text}
        })

    def close_text(self):
        if self.text_open:
            self._send_event('content_block_stop', {'type': 'content_block_stop', 'index': self.block_idx})
            self.text_open = False
            self.block_idx += 1

    def open_tool(self, tool_id, name):
        if self.text_open: 
            self.close_text()
        if self.tool_open: 
            self.close_tool() 
        self._send_event('content_block_start', {
            'type': 'content_block_start', 'index': self.block_idx, 
            'content_block': {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {}}
        })
        self.tool_open = True
        self.has_tool_use = True

    def send_tool_delta(self, args_json_str):
        if not self.tool_open or not args_json_str: 
            return
        self._send_event('content_block_delta', {
            'type': 'content_block_delta', 'index': self.block_idx, 
            'delta': {'type': 'input_json_delta', 'partial_json': args_json_str}
        })

    def close_tool(self):
        if self.tool_open:
            self._send_event('content_block_stop', {'type': 'content_block_stop', 'index': self.block_idx})
            self.tool_open = False
            self.block_idx += 1

    def inject_recovery_bash(self, reason="未知错误或提前中断"):
        if not self.is_agent_mode or self.has_tool_use: 
            return
        
        logging.warning(f"🚨 [Proxy-Bridge] 触发自动恢复: {reason}")
        self.close_text()
        
        warn_msg = (
            f"\n\n[🚨 **Proxy-Bridge 自动恢复** 🚨]\n"
            f"下游 LLM 未能成功生成有效的工具调用。\n"
            f"**原因**: {reason}\n"
            f"Proxy-Bridge 已拦截此结束信号并强制注入恢复循环。\n"
            f"请检查上下文，调整策略（如分块写入、简化 JSON 转义），重新尝试调用工具。不要停止任务！\n\n"
        )
        self.send_text_delta(warn_msg)
        self.close_text()
        
        tool_id = gen_tool_id()
        recovery_cmd = f"echo '[Proxy-Bridge 恢复信号] {reason}。请重试上一个工具调用。'"
        recovery_input = {"command": recovery_cmd}
        
        self.open_tool(tool_id, "bash")
        self.send_tool_delta(json.dumps(recovery_input, ensure_ascii=False))
        self.close_tool()

    def finish(self, upstream_stop_reason="stop"):
        self.close_text()
        self.close_tool()
        
        # 🚨 核心防停工判断：结合上游结束原因和文本完成标志
        if self.is_agent_mode and not self.has_tool_use:
            if is_task_completed(self.generated_text, upstream_stop_reason):
                logging.info("✅ [Proxy-Bridge] 检测到任务已完成标志，正常结束，不注入恢复信号。")
            else:
                reason = "下游 LLM 未生成 Tool Use 且无完成标志 (可能是 JSON 转义困难)"
                if upstream_stop_reason == 'length':
                    reason = "下游 LLM 触发长度限制 (stop_reason=length)"
                self.inject_recovery_bash(reason)
                
        stop_reason = "tool_use" if self.has_tool_use else "end_turn"
        self._send_event('message_delta', {
            'type': 'message_delta', 
            'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 
            'usage': {'output_tokens': 0}
        })
        self._send_event('message_stop', {'type': 'message_stop'})

# ==========================================
# 模块 3: 消息协议转换
# ==========================================
def convert_to_openai_req(anthropic_req, current_llm, valid_tools):
    openai_req = {
        "model": current_llm.get("model_name"), 
        "messages": [], 
        "stream": anthropic_req.get('stream', False)
    }
    for p in ["temperature", "top_p", "max_tokens", "stop_sequences"]:
        if p in anthropic_req: 
            openai_req[p if p != "stop_sequences" else "stop"] = anthropic_req[p]
            
    if "tools" in anthropic_req and anthropic_req["tools"]:
        openai_req["tools"] = [
            {
                "type": "function", 
                "function": {
                    "name": t["name"], 
                    "description": t.get("description", ""), 
                    "parameters": t.get("input_schema", {})
                }
            } for t in anthropic_req["tools"]
        ]

    sys_content = ""
    if "system" in anthropic_req:
        sys_val = anthropic_req["system"]
        sys_content = " ".join([b.get("text", "") for b in sys_val if b.get("type") == "text"]) if isinstance(sys_val, list) else sys_val
        
    if valid_tools: 
        sys_content += "\n\n[Tools Instruction]\nIf you cannot use Native API function calling, output EXACTLY:\n<tool_call>\n{\"name\": \"tool_name\", \"arguments\": {\"arg\": \"val\"}}\n</tool_call>"
        
    if sys_content: 
        openai_req["messages"].append({"role": "system", "content": sys_content})

    for m in anthropic_req.get("messages", []):
        role, content = m["role"], m["content"]
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
                            openai_req["messages"].append({"role": "user", "content": " ".join(text_parts)})
                            text_parts = []
                        res = block.get("content", "")
                        if isinstance(res, list): 
                            res = " ".join([str(b.get("text", b)) for b in res if b.get("type") == "text"])
                        if block.get("is_error"): 
                            res = f"Error: {res}"
                        openai_req["messages"].append({"role": "tool", "tool_call_id": block.get("tool_use_id", "unknown"), "content": res})
                if text_parts: 
                    openai_req["messages"].append({"role": "user", "content": " ".join(text_parts)})
                    
            elif role == "assistant":
                text_parts, tool_calls = [], []
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
                if text_parts or tool_calls:
                    msg = {"role": "assistant"}
                    if text_parts: 
                        msg["content"] = " ".join(text_parts)
                    if tool_calls: 
                        msg["tool_calls"] = tool_calls
                    openai_req["messages"].append(msg)
    return openai_req

# ==========================================
# 模块 4: 主路由与流式处理
# ==========================================
def handle_anthropic_api(sock, method, url, headers, body_prefix):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        body = body_prefix
        while len(body) < content_length:
            chunk = sock.recv(8192)
            if not chunk: 
                break
            body += chunk
            
        if not body: 
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nEmpty Body")
            return
            
        try: 
            anthropic_req = json.loads(body.decode('utf-8'))
        except Exception: 
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nInvalid JSON")
            return

        current_llm = utils.LLMS_CONFIG.get(utils.ACTIVE_LLM_KEY)
        if not current_llm: 
            sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\nNo LLM")
            return

        valid_tools = {t["name"]: t for t in anthropic_req.get("tools", [])}
        is_agent_mode = bool(valid_tools)
        
        valid_triggers = {
            "<tool_call>": "</tool_call>", 
            f"{MD_FENCE}json": MD_FENCE, 
            f"{MD_FENCE}tool_call": MD_FENCE
        }
        for t in valid_tools:
            valid_triggers[f"<{t.lower()}>"] = f"</{t.lower()}>"
            valid_triggers[f"<{t}>"] = f"</{t}>"
        
        closing_raw, stray_raw = get_garbage_lists()
        CLOSING_GARBAGE = list(closing_raw)
        STRAY_GARBAGE = list(stray_raw)

        openai_req = convert_to_openai_req(anthropic_req, current_llm, valid_tools)
        openai_req["messages"] = compress_messages(openai_req["messages"])
        
        base_url = current_llm.get("base_url", "")
        if not base_url.endswith("/chat/completions"): 
            base_url = base_url.rstrip("/") + "/chat/completions"
        
        ctx_ssl = ssl.create_default_context()
        if not current_llm.get("verify_ssl", True): 
            ctx_ssl.check_hostname = False
            ctx_ssl.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(base_url, data=json.dumps(openai_req).encode('utf-8'), method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'text/event-stream' if anthropic_req.get('stream') else 'application/json')
        api_key = current_llm.get("api_key", "")
        if api_key and api_key.lower() != "none": 
            req.add_header(current_llm.get("auth_header", "Authorization"), f"{current_llm.get('auth_prefix', 'Bearer ')}{api_key}")

        msg_id = f"msg_{int(time.time())}"
        
        try:
            with urllib.request.urlopen(req, timeout=900, context=ctx_ssl) as response:
                if anthropic_req.get('stream'):
                    sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
                    sse_ctx = StreamContext(sock, msg_id, openai_req['model'], is_agent_mode)
                    
                    text_buffer, is_intercepting, intercept_buffer, active_close_tag = "", False, "", ""
                    active_native_tools = {}
                    finish_reason = None  # 🚨 捕获上游结束原因

                    for line in response:
                        line = line.decode('utf-8').strip()
                        if not line or not line.startswith("data: "): 
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]": 
                            break
                        
                        try:
                            chunk = json.loads(data_str)
                            if "error" in chunk: 
                                continue
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            
                            # 🚨 捕获 finish_reason
                            fr = choice.get("finish_reason")
                            if fr: 
                                finish_reason = fr
                            
                            if reasoning := delta.get("reasoning_content"): 
                                sse_ctx.send_text_delta(reasoning)
                            
                            if content := delta.get("content"):
                                if is_intercepting:
                                    intercept_buffer += content
                                    if active_close_tag in intercept_buffer:
                                        close_idx = intercept_buffer.find(active_close_tag) + len(active_close_tag)
                                        full_xml = intercept_buffer[:close_idx]
                                        text_buffer = intercept_buffer[close_idx:]
                                        
                                        t_name, t_args = parse_fallback_tool(full_xml, valid_tools)
                                        if t_name != "unknown":
                                            sse_ctx.open_tool(gen_tool_id(), t_name)
                                            sse_ctx.send_tool_delta(json.dumps(t_args, ensure_ascii=False) if t_args else "{}")
                                            sse_ctx.close_tool()
                                        else:
                                            sse_ctx.inject_recovery_bash("拦截到无效的工具标签格式 (XML/JSON 损坏)")
                                        is_intercepting, intercept_buffer = False, ""
                                else:
                                    text_buffer += content
                                    for g in CLOSING_GARBAGE: 
                                        text_buffer = text_buffer.replace(g, "")
                                    
                                    matched_tag, earliest_idx = None, -1
                                    for tag in valid_triggers:
                                        if (idx := text_buffer.find(tag)) != -1 and (earliest_idx == -1 or idx < earliest_idx): 
                                            earliest_idx, matched_tag = idx, tag
                                    
                                    if matched_tag:
                                        if pre_text := text_buffer[:earliest_idx]: 
                                            sse_ctx.send_text_delta(pre_text)
                                        is_intercepting, active_close_tag, intercept_buffer = True, valid_triggers[matched_tag], text_buffer[earliest_idx:]
                                        text_buffer = ""
                                    else:
                                        if len(text_buffer) > 35:
                                            sse_ctx.send_text_delta(text_buffer[:-35])
                                            text_buffer = text_buffer[-35:]

                            for tc in delta.get("tool_calls", []):
                                idx = tc.get("index")
                                if idx is None: 
                                    continue
                                if idx not in active_native_tools:
                                    sse_ctx.open_tool(tc.get("id", gen_tool_id()), tc.get("function", {}).get("name", "unknown"))
                                    active_native_tools[idx] = sse_ctx.block_idx - 1 
                                
                                if args_delta := tc.get("function", {}).get("arguments"):
                                    sse_ctx.send_tool_delta(args_delta)
                        except Exception: 
                            pass
                    
                    if is_intercepting:
                        t_name, t_args = parse_fallback_tool(intercept_buffer, valid_tools)
                        if t_name != "unknown":
                            sse_ctx.open_tool(gen_tool_id(), t_name)
                            sse_ctx.send_tool_delta(json.dumps(t_args, ensure_ascii=False) if t_args else "{}")
                            sse_ctx.close_tool()
                    
                    if text_buffer:
                        for g in STRAY_GARBAGE: 
                            text_buffer = text_buffer.replace(g, "")
                        if text_buffer: 
                            sse_ctx.send_text_delta(text_buffer)
                    
                    for _ in active_native_tools: 
                        sse_ctx.close_tool()
                    
                    # 🚨 核心：传入 finish_reason 让状态机自动判断是否需要 bash 恢复
                    sse_ctx.finish(finish_reason or "stop")
                    
                else:
                    res_body = response.read()
                    res_data = json.loads(res_body)
                    if "error" in res_data:
                        sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\n" + json.dumps(res_data["error"]).encode())
                        return
                    
                    choice = res_data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    finish_reason = choice.get("finish_reason", "stop")
                    full_text = (msg.get("reasoning_content", "") + "\n\n" + (msg.get("content") or "")).strip()
                    
                    resp = {
                        "id": msg_id, "type": "message", "role": "assistant", 
                        "model": openai_req["model"], "content": [], 
                        "stop_reason": "end_turn", "stop_sequence": None, 
                        "usage": res_data.get("usage", {"input_tokens": 0, "output_tokens": 0})
                    }
                    if full_text.strip(): 
                        resp["content"].append({"type": "text", "text": full_text.strip()})
                    
                    for tc in msg.get("tool_calls", []):
                        try: 
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except: 
                            args = {}
                        resp["content"].append({"type": "tool_use", "id": tc.get("id", gen_tool_id()), "name": tc["function"]["name"], "input": args})
                        resp["stop_reason"] = "tool_use"
                    
                    # 🚨 非流式自动恢复判断
                    if is_agent_mode and resp["stop_reason"] != "tool_use":
                        if is_task_completed(full_text, finish_reason):
                            logging.info("✅ [Proxy-Bridge] 非流式：检测到任务已完成标志，正常结束。")
                        else:
                            warn_msg = "\n\n[🚨 **Proxy-Bridge 自动恢复** 🚨]\n下游 LLM 未生成 Tool Use 且无完成标志。已强制拦截并恢复循环。请调整策略重试。\n\n"
                            resp["content"].append({"type": "text", "text": warn_msg})
                            recovery_cmd = "echo '[Proxy-Bridge 恢复信号] LLM 未生成工具调用。请重试。'"
                            resp["content"].append({"type": "tool_use", "id": gen_tool_id(), "name": "bash", "input": {"command": recovery_cmd}})
                            resp["stop_reason"] = "tool_use"

                    if not resp["content"]: 
                        resp["content"].append({"type": "text", "text": ""})
                        
                    body_out = json.dumps(resp).encode('utf-8')
                    sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
                    
        except Exception as e:
            logging.error(f"🚨 [API] 请求失败: {e}")
            if not sock.fileno(): 
                return
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            sse_ctx = StreamContext(sock, msg_id, openai_req.get('model', 'unknown'), is_agent_mode)
            sse_ctx.send_text_delta(f"\n\n[Proxy-Bridge Error] Upstream API failed: {str(e)}\n\n")
            # 异常中断传入 error，强制触发 bash 恢复
            sse_ctx.finish("error")

    except Exception as e:
        logging.error(f"❌ 代理层致命错误: {e}")
        try: 
            sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\n")
        except: 
            pass
    finally:
        try: 
            sock.close()
        except: 
            pass