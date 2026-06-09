# 完整实现 Claude Code 原生接口转 OpenAI 协议功能
# 采用自然语言 Prompt + Pydantic 强制约束的降维打击方案，彻底无视下游 LLM 原生 Tool Calling 支持情况！
import json
import logging
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

import utils

try:
    from pydantic import BaseModel, ValidationError
except ImportError:
    logging.error("❌ 缺少 pydantic 库。请执行: pip install pydantic")
    sys.exit(1)


# ================= 定义 Pydantic 数据模型约束 =================
class ToolCallModel(BaseModel):
    name: str
    arguments: dict


def fix_json_backslashes(json_str):
    return re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', json_str)


def extract_tools_via_pydantic(text: str, valid_tools: dict):
    """
    通过正则匹配并利用 Pydantic 对拦截到的工具调用进行严格校验。
    新增返回项：has_error 和 error_msg 用于触发上层重试逻辑。
    """
    tools = []
    pattern = re.compile(r"```tool_call\s*(.*?)\s*```", re.DOTALL)
    has_error = False
    error_msg = ""

    def replace_func(match):
        nonlocal has_error, error_msg
        json_str = match.group(1)
        try:
            fixed_json = fix_json_backslashes(json_str)
            parsed = ToolCallModel.model_validate_json(fixed_json)
            # 只有当工具名称在允许列表中时，才认定为有效调用
            if parsed.name in valid_tools:
                tools.append({
                    "name": parsed.name,
                    "arguments": parsed.arguments
                })
                return ""  # 从文本中彻底移除这块内容，使得发送给用户的文字变干净
        except ValidationError as e:
            logging.warning(f"⚠️ 拦截到非法的工具格式 (Pydantic 校验未通过): {e}")
            has_error = True
            error_msg = f"Pydantic Validation Error: {e}"
        except Exception as e:
            logging.warning(f"⚠️ 拦截到损坏的 JSON 块: {e}")
            has_error = True
            error_msg = f"JSON Parse Error: {e}"
        return match.group(0)  # 校验失败则保留原文展示

    clean_text = pattern.sub(replace_func, text).strip()

    # 清理偶尔可能残留的末尾垃圾字符
    garbage = ["```json", "```", "<tool_calls_end>", "</tool_call>", "<tool_call>"]
    for g in garbage:
        clean_text = clean_text.replace(g, "")

    return clean_text.strip(), tools, has_error, error_msg


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

        is_stream = anthropic_req.get('stream', False)
        current_llm = utils.LLMS_CONFIG.get(utils.ACTIVE_LLM_KEY)
        if not current_llm:
            sock.sendall(b"HTTP/1.1 500 Internal Error\r\n\r\nNo LLM config")
            return

        openai_req = {"model": current_llm.get("model_name", "default_model"), "messages": [], "stream": is_stream}

        for param in ["max_tokens", "temperature", "top_p"]:
            if param in anthropic_req: openai_req[param] = anthropic_req[param]
        if "max_tokens" not in openai_req: openai_req["max_tokens"] = 8192

        # ================= 核心突破：工具降维为纯文本 Prompt =================
        valid_tools = {}
        tool_prompt = ""
        if "tools" in anthropic_req and len(anthropic_req["tools"]) > 0:
            tool_prompt = "\n\n=== TOOL USAGE INSTRUCTIONS ===\n"
            tool_prompt += "You have access to the following tools. You can use them to interact with the system or fetch information.\n"
            for t in anthropic_req["tools"]:
                valid_tools[t["name"]] = t
                tool_prompt += f"\nTool: {t['name']}\nDescription: {t.get('description', '')}\nSchema: {json.dumps(t.get('input_schema', {}), ensure_ascii=False)}\n"

            tool_prompt += """
\nTo use a tool, you MUST reply with a markdown code block starting with ` ```tool_call ` and containing a single valid JSON object.
The JSON object MUST conform to the schema of the tool and include exactly two fields: "name" and "arguments".
Example:
```tool_call
{"name": "read_file", "arguments": {"file_path": "main.py"}}
```
You may use multiple tools by providing multiple ```tool_call blocks.
If you don't need to use any tool, just answer normally with text.
========================================
"""
        # 注意：这里我们绝对不把 tools 放进 openai_req 中，强制要求下游 API 走文本推理

        # 处理系统消息并注入 Tool Prompt
        sys_content = ""
        if "system" in anthropic_req:
            sys_content = anthropic_req["system"]
            if isinstance(sys_content, list):
                sys_content = "".join([b.get("text", "") for b in sys_content if b.get("type") == "text"])

        if tool_prompt:
            sys_content += tool_prompt

        if sys_content:
            openai_req["messages"].append({"role": "system", "content": sys_content})

        # 拉平历史记录：将 Claude 的复杂组件消息，转译成纯文本上下文对话
        for m in anthropic_req.get("messages", []):
            role = m["role"]
            content = m["content"]
            if isinstance(content, str):
                openai_req["messages"].append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        # 将历史中的调用记录伪装成模型过去的输出格式
                        tc_json = json.dumps({"name": block["name"], "arguments": block["input"]}, ensure_ascii=False)
                        text_parts.append(f"```tool_call\n{tc_json}\n```")
                    elif block.get("type") == "tool_result":
                        res_content = block.get("content", "")
                        if isinstance(res_content, list):
                            res_content = "".join(
                                [str(b.get("text", b)) for b in res_content if b.get("type") == "text"])
                        if block.get("is_error"):
                            res_content = f"Error: {res_content}"
                        # 插入用户返回的工具执行结果
                        text_parts.append(
                            f"\n[Tool Execution Result for ID: {block.get('tool_use_id', 'unknown')}]\n{res_content}\n")

                if text_parts:
                    openai_req["messages"].append({"role": role, "content": "".join(text_parts)})

        api_key = current_llm.get("api_key", "")
        base_url = current_llm.get("base_url", "")
        auth_header_key = current_llm.get("auth_header", "Authorization")
        auth_header_prefix = current_llm.get("auth_prefix", "Bearer ")

        if not base_url.endswith("/chat/completions"): base_url = base_url.rstrip("/") + "/chat/completions"

        ctx = ssl.create_default_context()
        if not current_llm.get("verify_ssl", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        logging.info(
            f"🤖 [API] [{utils.ACTIVE_LLM_KEY}] 代理重组请求 (纯文本劫持模式): {anthropic_req.get('model', 'unknown')} -> {openai_req['model']}")

        # ================= 带有 2 次重试机制的主循环 =================
        max_retries = 3
        headers_sent = False
        anthropic_block_index = 0
        in_text_block = False
        msg_id = "msg_" + str(int(time.time()))

        for attempt in range(max_retries):
            # 每次请求前重新 dump，因为如果发生重试，我们可能追加了纠错提示
            req_body = json.dumps(openai_req).encode('utf-8')
            req = urllib.request.Request(base_url, data=req_body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Accept', 'application/json')
            req.add_header('User-Agent', 'OpenAI/Python')
            if api_key and api_key.lower() != "none": req.add_header(auth_header_key, f"{auth_header_prefix}{api_key}")

            try:
                # 提示：将 timeout 提高到 600 秒，以防 DeepSeek 思考超时
                with urllib.request.urlopen(req, timeout=600, context=ctx) as response:
                    if is_stream:
                        if not headers_sent:
                            sock.sendall(
                                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n".encode(
                                    'utf-8'))
                            # 提取为独立变量，避免 Python f-string 反斜杠语法错误
                            msg_start_payload = {
                                'type': 'message_start',
                                'message': {
                                    'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [],
                                    'model': openai_req['model'], 'stop_reason': None, 'stop_sequence': None,
                                    'usage': {'input_tokens': 0, 'output_tokens': 0}
                                }
                            }
                            sock.sendall(
                                f"event: message_start\ndata: {json.dumps(msg_start_payload)}\n\n".encode('utf-8'))
                            headers_sent = True

                        content_buffer = ""

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

                                    # 将深度思考/推理内容立刻流式打印
                                    reasoning = delta.get("reasoning_content", "")
                                    if reasoning:
                                        if not in_text_block:
                                            cb_start_payload = {'type': 'content_block_start',
                                                                'index': anthropic_block_index,
                                                                'content_block': {'type': 'text', 'text': ''}}
                                            sock.sendall(
                                                f"event: content_block_start\ndata: {json.dumps(cb_start_payload)}\n\n".encode(
                                                    'utf-8'))
                                            in_text_block = True

                                        cb_delta_payload = {'type': 'content_block_delta',
                                                            'index': anthropic_block_index,
                                                            'delta': {'type': 'text_delta', 'text': reasoning}}
                                        sock.sendall(
                                            f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode(
                                                'utf-8'))

                                    # 核心内容缓存，不再向外暴露出包含 ```tool_call 的原文
                                    content = delta.get("content", "")
                                    if content: content_buffer += content

                                except Exception:
                                    pass

                        # ====== 流式输出收集完毕：提取、校验 ======
                        clean_text, hidden_tools, has_error, err_msg = extract_tools_via_pydantic(content_buffer,
                                                                                                  valid_tools)

                        # 核心重试判断
                        if has_error and attempt < max_retries - 1:
                            logging.warning(
                                f"🔄 [API] 拦截到非法工具格式，正在执行第 {attempt + 2}/{max_retries} 次重试...")

                            # 向用户侧静默发送正在重试的提示，保持流不中断
                            if not in_text_block:
                                cb_start_payload = {'type': 'content_block_start', 'index': anthropic_block_index,
                                                    'content_block': {'type': 'text', 'text': ''}}
                                sock.sendall(
                                    f"event: content_block_start\ndata: {json.dumps(cb_start_payload)}\n\n".encode(
                                        'utf-8'))
                                in_text_block = True

                            retry_notice = f"\n\n[🔄 System: Intercepted invalid tool call JSON. Retrying attempt {attempt + 2}/{max_retries}...]\n\n"
                            cb_delta_payload = {'type': 'content_block_delta', 'index': anthropic_block_index,
                                                'delta': {'type': 'text_delta', 'text': retry_notice}}
                            sock.sendall(
                                f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode('utf-8'))

                            # 把坏掉的原文和报错教训塞进下一次请求的对话历史里
                            openai_req["messages"].append({"role": "assistant", "content": content_buffer})
                            openai_req["messages"].append({"role": "user",
                                                           "content": f"Your previous tool call failed JSON validation. Error details: {err_msg}. Please fix the JSON syntax (e.g., escape backslashes like \\\\ if using Windows paths) and output the valid tool call again."})
                            continue  # 直接进入下一轮 API 请求！

                        # 如果没有错误（或者已经是最后一次尝试），则彻底输出结果
                        if clean_text:
                            if not in_text_block:
                                cb_start_payload = {'type': 'content_block_start', 'index': anthropic_block_index,
                                                    'content_block': {'type': 'text', 'text': ''}}
                                sock.sendall(
                                    f"event: content_block_start\ndata: {json.dumps(cb_start_payload)}\n\n".encode(
                                        'utf-8'))
                                in_text_block = True

                                cb_delta_payload = {'type': 'content_block_delta', 'index': anthropic_block_index,
                                                    'delta': {'type': 'text_delta', 'text': clean_text}}
                                sock.sendall(
                                    f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode(
                                        'utf-8'))
                            else:
                                # 彻底剥离了 F-string 内的反斜杠，解决 Python 版本引发的 SyntaxError
                                cb_delta_payload = {'type': 'content_block_delta', 'index': anthropic_block_index,
                                                    'delta': {'type': 'text_delta', 'text': '\n\n' + clean_text}}
                                sock.sendall(
                                    f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode(
                                        'utf-8'))

                        if in_text_block:
                            cb_stop_payload = {'type': 'content_block_stop', 'index': anthropic_block_index}
                            sock.sendall(
                                f"event: content_block_stop\ndata: {json.dumps(cb_stop_payload)}\n\n".encode('utf-8'))
                            anthropic_block_index += 1

                        stop_reason = "end_turn"
                        if hidden_tools:
                            stop_reason = "tool_use"

                        for ht in hidden_tools:
                            tool_id = f'call_{int(time.time())}_{anthropic_block_index}'
                            # 开启 tool_use 块
                            tu_start_payload = {'type': 'content_block_start', 'index': anthropic_block_index,
                                                'content_block': {'type': 'tool_use', 'id': tool_id, 'name': ht['name'],
                                                                  'input': {}}}
                            sock.sendall(
                                f"event: content_block_start\ndata: {json.dumps(tu_start_payload)}\n\n".encode('utf-8'))

                            # 发送经过 Pydantic 过滤确认安全的字典对应的 JSON 字符串
                            if ht['arguments']:
                                safe_args_str = json.dumps(ht['arguments'], ensure_ascii=False)
                                tu_delta_payload = {'type': 'content_block_delta', 'index': anthropic_block_index,
                                                    'delta': {'type': 'input_json_delta',
                                                              'partial_json': safe_args_str}}
                                sock.sendall(
                                    f"event: content_block_delta\ndata: {json.dumps(tu_delta_payload)}\n\n".encode(
                                        'utf-8'))

                            # 关闭块
                            tu_stop_payload = {'type': 'content_block_stop', 'index': anthropic_block_index}
                            sock.sendall(
                                f"event: content_block_stop\ndata: {json.dumps(tu_stop_payload)}\n\n".encode('utf-8'))
                            anthropic_block_index += 1

                        msg_delta_payload = {'type': 'message_delta',
                                             'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                                             'usage': {'output_tokens': 0}}
                        sock.sendall(f"event: message_delta\ndata: {json.dumps(msg_delta_payload)}\n\n".encode('utf-8'))
                        sock.sendall(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
                        break  # 跳出重试循环，请求处理完毕

                    else:  # 非流式模式 (Non-stream)
                        res_body = response.read()
                        try:
                            openai_data = json.loads(res_body)
                            if "error" in openai_data:
                                err_msg = json.dumps(openai_data).encode('utf-8')
                                sock.sendall(
                                    f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(err_msg)}\r\n\r\n".encode(
                                        'utf-8') + err_msg)
                                return

                            anthropic_resp = {"id": "msg_" + str(int(time.time())), "type": "message",
                                              "role": "assistant", "content": [], "model": openai_req["model"],
                                              "stop_reason": "end_turn", "stop_sequence": None,
                                              "usage": openai_data.get("usage",
                                                                       {"input_tokens": 0, "output_tokens": 0})}
                            msg = openai_data.get("choices", [{}])[0].get("message", {})

                            full_text = ((msg.get("reasoning_content") or "") + "\n\n" + (
                                    msg.get("content") or "")).strip()
                            clean_text, hidden_tools, has_error, err_msg = extract_tools_via_pydantic(full_text,
                                                                                                      valid_tools)

                            # 核心重试判断
                            if has_error and attempt < max_retries - 1:
                                logging.warning(
                                    f"🔄 [API] 拦截到非法工具格式，正在执行第 {attempt + 2}/{max_retries} 次重试...")
                                openai_req["messages"].append({"role": "assistant", "content": full_text})
                                openai_req["messages"].append({"role": "user",
                                                               "content": f"Your previous tool call failed JSON validation. Error details: {err_msg}. Please fix the JSON syntax (e.g., escape backslashes like \\\\ if using Windows paths) and output the valid tool call again."})
                                continue

                            if clean_text:
                                anthropic_resp["content"].append({"type": "text", "text": clean_text})

                            for ht in hidden_tools:
                                anthropic_resp["content"].append({
                                    "type": "tool_use",
                                    "id": f"call_{int(time.time())}_{len(anthropic_resp['content'])}",
                                    "name": ht['name'],
                                    "input": ht['arguments']
                                })
                                anthropic_resp["stop_reason"] = "tool_use"

                            if not anthropic_resp["content"]: anthropic_resp["content"].append(
                                {"type": "text", "text": ""})

                            body_out = json.dumps(anthropic_resp).encode('utf-8')
                            sock.sendall(
                                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode(
                                    'utf-8') + body_out)
                            break  # 跳出重试循环

                        except Exception as inner_e:
                            sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                            break  # 如果是整个 JSON 架构垮掉，说明彻底坏了，直接中断

            except urllib.error.HTTPError as e:
                res_body = e.read()
                res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\n"
                for k, v in e.headers.items():
                    if k.lower() not in ['connection', 'transfer-encoding', 'content-encoding',
                                         'content-length']: res_head_str += f"{k}: {v}\r\n"
                res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                try:
                    sock.sendall(res_head_str.encode('utf-8') + res_body)
                except:
                    pass
                break  # 直接返回下游的 400 错误，不重试
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    logging.warning(f"🔄 [API] 网络连接失败: {e}，正在准备重试...")
                    time.sleep(2)
                    continue
                sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\nConnection Failed: {str(e)}".encode('utf-8'))
                break

    except Exception as e:
        try:
            sock.sendall(f"HTTP/1.1 500 Internal Error\r\n\r\n{str(e)}".encode('utf-8'))
        except:
            pass
    finally:
        try:
            sock.close()
        except:
            pass
