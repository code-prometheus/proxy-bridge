# Anthropic 协议代理核心 (严格遵循 CC Switch 原生映射版)
# 核心职责：对接下游 LLM、原生 Tool Calling 双向严格映射
# 约束：完全剥离正则截获与 Prompt 伪装，强制采用标准的 OpenAI/Anthropic Function Calling 协议双向映射！
import json
import logging
import ssl
import time
import urllib.error
import urllib.request

import utils
from inbound_compressor import compress_messages

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

        # 获取客户端是否请求流式 (Claude Code 通常是 True)
        is_stream = anthropic_req.get('stream', False)

        # ================= CC Switch 规范：严格原生协议透传与映射 =================
        openai_req = {
            "model": current_llm.get("model_name", "default_model"),
            "messages": [],
            "stream": is_stream
        }

        # 1. 基础参数映射
        if "temperature" in anthropic_req: openai_req["temperature"] = anthropic_req["temperature"]
        if "top_p" in anthropic_req: openai_req["top_p"] = anthropic_req["top_p"]
        if "max_tokens" in anthropic_req: openai_req["max_tokens"] = anthropic_req["max_tokens"]
        if "stop_sequences" in anthropic_req: openai_req["stop"] = anthropic_req["stop_sequences"]

        # 2. 原生 Tools 映射：抛弃伪装，直接将 input_schema 映射为 OpenAI parameters
        if "tools" in anthropic_req and len(anthropic_req["tools"]) > 0:
            openai_req["tools"] = []
            for t in anthropic_req["tools"]:
                openai_req["tools"].append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {})
                    }
                })

        # 3. System 映射
        if "system" in anthropic_req:
            sys_val = anthropic_req["system"]
            if isinstance(sys_val, list):
                sys_content = "".join([b.get("text", "") for b in sys_val if b.get("type") == "text"])
            else:
                sys_content = sys_val
            openai_req["messages"].append({"role": "system", "content": sys_content})

        # 4. Messages 对话历史严格双向映射 (兼容 user 与 assistant 的工具历史)
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
                            # OpenAI 规范：工具执行结果必须分离为单独的 role: "tool"
                            if text_parts:
                                openai_req["messages"].append({"role": "user", "content": "".join(text_parts)})
                                text_parts = []
                            res_content = block.get("content", "")
                            if isinstance(res_content, list):
                                res_content = "".join([str(b.get("text", b)) for b in res_content if b.get("type") == "text"])
                            if block.get("is_error"):
                                res_content = f"Error: {res_content}"
                            openai_req["messages"].append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
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
                            # Assistant 调用的工具必须转换为原生 tool_calls 结构体
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                                }
                            })
                    ast_msg = {"role": "assistant"}
                    if text_parts:
                        ast_msg["content"] = "".join(text_parts)
                    if tool_calls:
                        ast_msg["tool_calls"] = tool_calls
                    if text_parts or tool_calls:
                        openai_req["messages"].append(ast_msg)

        # 5. 调用独立模块进行无损上下文压缩
        openai_req["messages"] = compress_messages(openai_req["messages"])

        api_key = current_llm.get("api_key", "")
        base_url = current_llm.get("base_url", "")
        auth_header_key = current_llm.get("auth_header", "Authorization")
        auth_header_prefix = current_llm.get("auth_prefix", "Bearer ")

        if not base_url.endswith("/chat/completions"): 
            base_url = base_url.rstrip("/") + "/chat/completions"

        ctx = ssl.create_default_context()
        if not current_llm.get("verify_ssl", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        logging.info(f"🤖 [API] [{utils.ACTIVE_LLM_KEY}] 代理重组请求 (纯净原生结构映射): {anthropic_req.get('model', 'unknown')} -> {openai_req['model']}")

        max_retries = 3
        headers_sent = False
        msg_id = "msg_" + str(int(time.time()))

        for attempt in range(max_retries):
            req_body = json.dumps(openai_req, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(base_url, data=req_body, method='POST')
            req.add_header('Content-Type', 'application/json')
            if is_stream:
                req.add_header('Accept', 'text/event-stream')
            else:
                req.add_header('Accept', 'application/json')
            req.add_header('User-Agent', 'OpenAI/Python')
            if api_key and api_key.lower() != "none": 
                req.add_header(auth_header_key, f"{auth_header_prefix}{api_key}")

            try:
                logging.info(f"📡 [API] 发起请求，目标: {base_url}")
                with urllib.request.urlopen(req, timeout=600, context=ctx) as response:
                    
                    if is_stream:
                        if not headers_sent:
                            sock.sendall("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n".encode('utf-8'))
                            headers_sent = True

                        is_openai_format = None
                        anthropic_block_index = 0
                        in_text_block = False
                        active_tool_open = None
                        openai_tool_map = {}
                        has_tool_use = False

                        for line in response:
                            line = line.decode('utf-8').strip()
                            if not line: continue
                            if line.startswith("data: "):
                                data_str = line[6:]
                                
                                if data_str == "[DONE]": 
                                    break
                                    
                                try:
                                    chunk = json.loads(data_str)
                                    
                                    # ================= 动态侦测下游协议类型 =================
                                    if is_openai_format is None:
                                        if "type" in chunk and chunk["type"] in ["message_start", "content_block_start", "ping"]:
                                            # 原生 Anthropic 端点：原样透传
                                            is_openai_format = False
                                        else:
                                            # 原生 OpenAI 端点：启动翻译引擎，先发补全必须的 message_start
                                            is_openai_format = True
                                            msg_start_payload = {
                                                'type': 'message_start',
                                                'message': {
                                                    'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [],
                                                    'model': openai_req['model'], 'stop_reason': None, 'stop_sequence': None,
                                                    'usage': {'input_tokens': 0, 'output_tokens': 0}
                                                }
                                            }
                                            sock.sendall(f"event: message_start\ndata: {json.dumps(msg_start_payload)}\n\n".encode('utf-8'))
                                            
                                    if not is_openai_format:
                                        # 原生 Anthropic 事件无缝透传
                                        event_name = chunk.get("type", "message_delta")
                                        sock.sendall(f"event: {event_name}\ndata: {data_str}\n\n".encode('utf-8'))
                                        continue

                                    # ================= OpenAI -> Anthropic 结构化转换引擎 =================
                                    choices = chunk.get("choices", [])
                                    if not choices: continue
                                    delta = choices[0].get("delta", {})

                                    # 1. 纯文本及思维链映射
                                    reasoning = delta.get("reasoning_content", "")
                                    content = delta.get("content", "")
                                    text_chunk = reasoning + (content if content else "")
                                    
                                    if text_chunk:
                                        # 如果发生了工具误打断文本，依照规范安全关闭前序工具块
                                        if active_tool_open is not None:
                                            sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': active_tool_open})}\n\n".encode('utf-8'))
                                            active_tool_open = None
                                            
                                        if not in_text_block:
                                            cb_start = {'type': 'content_block_start', 'index': anthropic_block_index, 'content_block': {'type': 'text', 'text': ''}}
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode('utf-8'))
                                            in_text_block = True
                                            
                                        cb_delta = {'type': 'content_block_delta', 'index': anthropic_block_index, 'delta': {'type': 'text_delta', 'text': text_chunk}}
                                        sock.sendall(f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n".encode('utf-8'))

                                    # 2. 原生 OpenAI tool_calls 流映射为 Anthropic content_block
                                    tool_calls = delta.get("tool_calls", [])
                                    for tc in tool_calls:
                                        idx = tc.get("index")
                                        if idx is None: continue
                                        
                                        if idx not in openai_tool_map:
                                            # 新工具开始，安全关闭当前的文本块
                                            if in_text_block:
                                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anthropic_block_index})}\n\n".encode('utf-8'))
                                                in_text_block = False
                                                anthropic_block_index += 1
                                                
                                            # 安全关闭上一个并行的工具块
                                            if active_tool_open is not None:
                                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': active_tool_open})}\n\n".encode('utf-8'))
                                                
                                            openai_tool_map[idx] = anthropic_block_index
                                            active_tool_open = anthropic_block_index
                                            has_tool_use = True
                                            
                                            tool_id = tc.get("id", f"call_{int(time.time())}_{idx}")
                                            func_name = tc.get("function", {}).get("name", "unknown_tool")
                                            
                                            # 发射 tool_use 开始块
                                            tu_start = {
                                                'type': 'content_block_start',
                                                'index': anthropic_block_index,
                                                'content_block': {'type': 'tool_use', 'id': tool_id, 'name': func_name, 'input': {}}
                                            }
                                            sock.sendall(f"event: content_block_start\ndata: {json.dumps(tu_start)}\n\n".encode('utf-8'))
                                            anthropic_block_index += 1
                                            
                                        # 工具参数增量传输
                                        args_delta = tc.get("function", {}).get("arguments", "")
                                        if args_delta:
                                            curr_anth_idx = openai_tool_map[idx]
                                            tu_delta = {
                                                'type': 'content_block_delta',
                                                'index': curr_anth_idx,
                                                'delta': {'type': 'input_json_delta', 'partial_json': args_delta}
                                            }
                                            sock.sendall(f"event: content_block_delta\ndata: {json.dumps(tu_delta)}\n\n".encode('utf-8'))

                                except Exception:
                                    pass

                        # ====== 流式传输结束妥善收尾 ======
                        if is_openai_format:
                            if in_text_block:
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': anthropic_block_index})}\n\n".encode('utf-8'))
                            if active_tool_open is not None:
                                sock.sendall(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': active_tool_open})}\n\n".encode('utf-8'))
                                
                            stop_reason = "tool_use" if has_tool_use else "end_turn"
                            msg_delta_payload = {'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}}
                            sock.sendall(f"event: message_delta\ndata: {json.dumps(msg_delta_payload)}\n\n".encode('utf-8'))
                            sock.sendall(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
                            
                        logging.info("✅ [API] 流式会话转换与传输正常结束")
                        break
                    
                    else:
                        # ====== 非流式响应双向映射兜底 ======
                        res_body = response.read()
                        try:
                            res_data = json.loads(res_body)
                            if "error" in res_data:
                                err_msg = json.dumps(res_data).encode('utf-8')
                                sock.sendall(f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(err_msg)}\r\n\r\n".encode('utf-8') + err_msg)
                                return
                                
                            # 动态感知：已经是原生 Anthropic 非流式结构，直通返回
                            if "type" in res_data and res_data["type"] == "message":
                                sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(res_body)}\r\nConnection: close\r\n\r\n".encode('utf-8') + res_body)
                                break
                                
                            # 启动非流式映射包装
                            msg = res_data.get("choices", [{}])[0].get("message", {})
                            anthropic_resp = {
                                "id": msg_id, "type": "message", "role": "assistant",
                                "model": openai_req["model"], "content": [], "stop_reason": "end_turn",
                                "stop_sequence": None, "usage": res_data.get("usage", {"input_tokens": 0, "output_tokens": 0})
                            }
                            
                            reasoning = msg.get("reasoning_content", "")
                            content = msg.get("content", "")
                            full_text = (reasoning + (content if content else "")).strip()
                            
                            if full_text:
                                anthropic_resp["content"].append({"type": "text", "text": full_text})
                                
                            openai_tools = msg.get("tool_calls", [])
                            for tc in openai_tools:
                                func = tc.get("function", {})
                                try: args = json.loads(func.get("arguments", "{}"))
                                except: args = {}
                                anthropic_resp["content"].append({
                                    "type": "tool_use",
                                    "id": tc.get("id", f"call_{int(time.time())}"),
                                    "name": func.get("name"),
                                    "input": args
                                })
                                anthropic_resp["stop_reason"] = "tool_use"
                                
                            if not anthropic_resp["content"]:
                                anthropic_resp["content"].append({"type": "text", "text": ""})
                                
                            body_out = json.dumps(anthropic_resp).encode('utf-8')
                            sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
                            logging.info("✅ [API] 非流式会话转换正常结束")
                            break
                            
                        except Exception as inner_e:
                            sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                            break

            except urllib.error.HTTPError as e:
                res_body = e.read()
                logging.error(f"❌ [API] HTTP 错误 {e.code}: {e.reason}")
                res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\n"
                for k, v in e.headers.items():
                    if k.lower() not in ['connection', 'transfer-encoding', 'content-encoding', 'content-length']:
                        res_head_str += f"{k}: {v}\r\n"
                res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                try: sock.sendall(res_head_str.encode('utf-8') + res_body)
                except: pass
                break
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    logging.warning(f"🔄 [API] 网络连接失败: {e}，正在重试 ({attempt+1}/{max_retries})...")
                    time.sleep(2)
                    continue
                logging.error(f"❌ [API] 彻底连接失败: {e}")
                sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\nConnection Failed: {str(e)}".encode('utf-8'))
                break
            except Exception as outer_e:
                logging.error(f"❌ [API] 代理发生内部意外异常: {outer_e}")
                break

    except Exception as e:
        logging.error(f"❌ [API] 发生致命错误: {e}")
        try: sock.sendall(f"HTTP/1.1 500 Internal Error\r\n\r\n{str(e)}".encode('utf-8'))
        except: pass
    finally:
        try: sock.close()
        except: pass