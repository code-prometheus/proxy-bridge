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


def fix_llm_json_str(json_str):
    """
    专门针对 LLM 容易生成的残缺 JSON 进行高强度预清洗与自愈合，大幅降低重试率
    """
    json_str = json_str.strip()
    # 1. 剥离 LLM 有时画蛇添足加上的嵌套 markdown 标签
    if json_str.startswith("```json"):
        json_str = json_str[7:]
    if json_str.endswith("```"):
        json_str = json_str[:-3]
    json_str = json_str.strip()

    # 2. 修复非法的单引号转义 (LLM 常在写代码参数时输出 \')
    json_str = json_str.replace(r"\'", "'")

    # 3. 修复末尾多余的逗号 (Trailing commas)
    json_str = re.sub(r',\s*([\]}])', r'\1', json_str)

    # 4. 终极自愈合：利用 Python 内置解析器的准确定位进行定向抢救
    # 扩容至 5000 次，确保上千行的代码块有足够的额度把所有反斜杠修复完毕
    for _ in range(5000):
        try:
            json.loads(json_str, strict=False)
            break  # 如果解析成功，立刻跳出自愈循环
        except json.JSONDecodeError as e:
            if "Invalid \\escape" in e.msg or "Invalid \\uXXXX escape" in e.msg:
                # 定向修复非法反斜杠：在 e.pos 的位置再插入一个反斜杠强制使其合法化
                json_str = json_str[:e.pos] + '\\' + json_str[e.pos:]
            elif "Unterminated string" in e.msg:
                # 修复意外截断的字符串
                json_str += '"'
            else:
                # 遇到无法简单自愈的严重错位（如 LLM 使用了 @" 多行语法），直接退出交给外层重试
                break

    return json_str


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
            # 经过深度清洗与自愈合
            fixed_json = fix_llm_json_str(json_str)

            # 放弃严苛的 Pydantic 原生 JSON 解析，改用宽容的 Python 内置解析器
            parsed_dict = json.loads(fixed_json, strict=False)

            # 再交由 Pydantic 进行字典级别的数据架构校验
            parsed = ToolCallModel.model_validate(parsed_dict)

            # 只有当工具名称在允许列表中时，才认定为有效调用
            if parsed.name in valid_tools:
                tools.append({
                    "name": parsed.name,
                    "arguments": parsed.arguments
                })
                return ""  # 从文本中彻底移除这块内容，使得发送给用户的文字变干净
        except ValidationError as e:
            logging.warning(f"⚠️ 工具参数校验不匹配: {e}")
            has_error = True
            error_msg = f"Pydantic Validation Error: {e}"
        except Exception as e:
            logging.error(f"⚠️ 工具解析彻底失败: {e} | 原始残缺数据前200字: {json_str[:200]}...")
            has_error = True
            error_msg = f"JSON Parse Error: {e}"
        return match.group(0)  # 校验失败则保留原文展示，以便后续曝光

    clean_text = pattern.sub(replace_func, text).strip()

    # 只有在没有错误时，才清空 markdown 标签。一旦出错，保留原始内容以便重试或展示给用户
    if not has_error:
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
            tool_prompt = "\n\n[TOOLS]\n"
            for t in anthropic_req["tools"]:
                valid_tools[t["name"]] = t
                schema_str = json.dumps(t.get('input_schema', {}), ensure_ascii=False, separators=(',', ':'))
                tool_prompt += f"- {t['name']}: {t.get('description', '')}\n  Schema: {schema_str}\n"

            # 强硬遏制 LLM 自作聪明的行为（针对 @"" 语法和换行问题进行严厉约束）
            tool_prompt += """
[CRITICAL INSTRUCTION FOR TOOLS]
You MUST use this EXACT format for tools:
```tool_call
{"name": "tool_name", "arguments": {"arg": "val"}}
```
STRICT JSON RULES:
1. Must be valid JSON.
2. For multiline code/text, you MUST escape newlines as `\\n` and quotes as `\\"`.
3. NEVER use raw newlines, unescaped backslashes, or special string formats like `@"..."@` inside JSON.
4. If no tool is needed, answer normally.
"""

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

        # 拉平历史记录
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
                        tc_json = json.dumps({"name": block["name"], "arguments": block["input"]}, ensure_ascii=False)
                        text_parts.append(f"```tool_call\n{tc_json}\n```")
                    elif block.get("type") == "tool_result":
                        res_content = block.get("content", "")
                        if isinstance(res_content, list):
                            res_content = "".join(
                                [str(b.get("text", b)) for b in res_content if b.get("type") == "text"])
                        if block.get("is_error"):
                            res_content = f"Error: {res_content}"
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
            f"🤖 [API] [{utils.ACTIVE_LLM_KEY}] 代理重组请求: {anthropic_req.get('model', 'unknown')} -> {openai_req['model']}")

        max_retries = 3
        headers_sent = False
        anthropic_block_index = 0
        in_text_block = False
        msg_id = "msg_" + str(int(time.time()))

        for attempt in range(max_retries):
            req_body = json.dumps(openai_req).encode('utf-8')
            req = urllib.request.Request(base_url, data=req_body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Accept', 'application/json')
            req.add_header('User-Agent', 'OpenAI/Python')
            if api_key and api_key.lower() != "none": req.add_header(auth_header_key, f"{auth_header_prefix}{api_key}")

            try:
                with urllib.request.urlopen(req, timeout=600, context=ctx) as response:
                    if is_stream:
                        if not headers_sent:
                            sock.sendall(
                                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n".encode(
                                    'utf-8'))
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

                                    content = delta.get("content", "")
                                    if content:
                                        if not valid_tools:
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
                                                                'delta': {'type': 'text_delta', 'text': content}}
                                            sock.sendall(
                                                f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode(
                                                    'utf-8'))
                                        else:
                                            content_buffer += content

                                except Exception:
                                    pass

                        clean_text, hidden_tools, has_error, err_msg = extract_tools_via_pydantic(content_buffer,
                                                                                                  valid_tools)

                        if has_error and attempt < max_retries - 1:
                            logging.warning(
                                f"🔄 [API] 拦截到非法工具格式，正在执行第 {attempt + 2}/{max_retries} 次重试...")

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

                            openai_req["messages"].append({"role": "assistant", "content": content_buffer})
                            openai_req["messages"].append({"role": "user",
                                                           "content": f"Your previous tool call failed JSON validation. Error details: {err_msg}. Please fix the JSON syntax (e.g., escape backslashes like \\\\ if using Windows paths) and output the valid tool call again."})
                            continue

                        # 核心兜底逻辑：如果彻底失败不再重试了，就在返回内容的头部强制打上错误警告（让用户明确看到发生了什么，不再装死）
                        if clean_text:
                            if has_error:
                                warning_header = f"\n\n[⚠️ Proxy System Error: LLM 经过 {max_retries} 次尝试依然无法生成合法的 JSON 工具调用。以下为它的原始破损输出，请考虑更换模型或修改 Prompt。]\n\n"
                                clean_text = warning_header + clean_text

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
                                f"event: content_block_delta\ndata: {json.dumps(cb_delta_payload)}\n\n".encode('utf-8'))

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
                            tu_start_payload = {'type': 'content_block_start', 'index': anthropic_block_index,
                                                'content_block': {'type': 'tool_use', 'id': tool_id, 'name': ht['name'],
                                                                  'input': {}}}
                            sock.sendall(
                                f"event: content_block_start\ndata: {json.dumps(tu_start_payload)}\n\n".encode('utf-8'))

                            if ht['arguments']:
                                safe_args_str = json.dumps(ht['arguments'], ensure_ascii=False)
                                tu_delta_payload = {'type': 'content_block_delta', 'index': anthropic_block_index,
                                                    'delta': {'type': 'input_json_delta',
                                                              'partial_json': safe_args_str}}
                                sock.sendall(
                                    f"event: content_block_delta\ndata: {json.dumps(tu_delta_payload)}\n\n".encode(
                                        'utf-8'))

                            tu_stop_payload = {'type': 'content_block_stop', 'index': anthropic_block_index}
                            sock.sendall(
                                f"event: content_block_stop\ndata: {json.dumps(tu_stop_payload)}\n\n".encode('utf-8'))
                            anthropic_block_index += 1

                        msg_delta_payload = {'type': 'message_delta',
                                             'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                                             'usage': {'output_tokens': 0}}
                        sock.sendall(f"event: message_delta\ndata: {json.dumps(msg_delta_payload)}\n\n".encode('utf-8'))
                        sock.sendall(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
                        break

                    else:
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

                            if has_error and attempt < max_retries - 1:
                                logging.warning(
                                    f"🔄 [API] 拦截到非法工具格式，正在执行第 {attempt + 2}/{max_retries} 次重试...")
                                openai_req["messages"].append({"role": "assistant", "content": full_text})
                                openai_req["messages"].append({"role": "user",
                                                               "content": f"Your previous tool call failed JSON validation. Error details: {err_msg}. Please fix the JSON syntax (e.g., escape backslashes like \\\\ if using Windows paths) and output the valid tool call again."})
                                continue

                            # 同样对非流式开启强制曝光兜底
                            if clean_text:
                                if has_error:
                                    warning_header = f"\n\n[⚠️ Proxy System Error: LLM 经过 {max_retries} 次尝试依然无法生成合法的 JSON 工具调用。以下为它的原始破损输出，请考虑更换模型或修改 Prompt。]\n\n"
                                    clean_text = warning_header + clean_text
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
                            break

                        except Exception as inner_e:
                            sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                            break

            except urllib.error.HTTPError as e:
                res_body = e.read()
                res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\n"
                for k, v in e.headers.items():
                    if k.lower() not in ['connection', 'transfer-encoding', 'content-encoding', 'content-length']:
                        res_head_str += f"{k}: {v}\r\n"
                res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                try:
                    sock.sendall(res_head_str.encode('utf-8') + res_body)
                except:
                    pass
                break
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
