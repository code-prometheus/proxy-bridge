import logging
import socket
import struct
import threading
import time
import queue
import hashlib
import os
import sys
import json
import base64
import urllib.request
import urllib.error
import ssl
import datetime

# ==================== 确保标准输入输出为二进制模式 ====================
if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

# ==================== 日志劫持 ====================
log_file = os.path.join(os.path.dirname(__file__), 'super_bridge.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(log_file, 'a', 'utf-8'), logging.StreamHandler(sys.stderr)]
)

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:
    logging.error("❌ 缺少 cryptography 库。请执行: pip install cryptography")
    sys.exit(1)

# ==================== 核心配置 (JSON) ====================
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), 'settings.json')

try:
    with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
        settings = json.load(f)

    SERVER_ADDR = settings['client']['server_addr']
    SERVER_PORT = settings['client']['server_port']
    LOCAL_PROXY_IP = settings['client'].get('local_proxy_ip', '127.0.0.1')
    LOCAL_PROXY_PORT = settings['client'].get('local_proxy_port', 60130)
    SECRET_KEY = settings['common']['secret_key'].encode('utf-8')

    # --- LLM Adapter 配置 ---
    LLM_ENABLED = settings.get('llm_adapter', {}).get('enabled', False)
    LLM_API_BASE = settings.get('llm_adapter', {}).get('openai_api_base', 'http://127.0.0.1:11434/v1')
    LLM_API_KEY = settings.get('llm_adapter', {}).get('openai_api_key', 'ollama')
    LLM_MODEL = settings.get('llm_adapter', {}).get('openai_model', 'llama3')
    LLM_VERIFY_SSL = settings.get('llm_adapter', {}).get('verify_ssl', False)
    LLM_PROXY = settings.get('llm_adapter', {}).get('openai_proxy', None)
except Exception as e:
    logging.error(f"❌ 配置文件 settings.json 加载失败或缺少必要配置项: {e}")
    sys.exit(1)

CA_DIR = os.path.join(os.path.expanduser('~'), '.proxy-bridge-ca')
CERTS_DIR = os.path.join(CA_DIR, 'certs')
os.makedirs(CERTS_DIR, exist_ok=True)

CHROME_CONNECTED = False
nm_pending_requests = {}
nm_request_id_counter = 1
nm_lock = threading.Lock()

class CertManager:
    CA_CERT_PATH = os.path.join(CA_DIR, 'ca-cert.pem')
    CA_KEY_PATH = os.path.join(CA_DIR, 'ca-key.pem')

    @classmethod
    def get_ca(cls):
        if not os.path.exists(cls.CA_CERT_PATH) or not os.path.exists(cls.CA_KEY_PATH):
            logging.info("🔧 正在生成 Python 版 Proxy Bridge Local CA...")
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"Proxy Bridge Local CA")])
            cert = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(
                private_key.public_key()
            ).serial_number(x509.random_serial_number()).not_valid_before(
                datetime.datetime.utcnow() - datetime.timedelta(days=1)
            ).not_valid_after(
                datetime.datetime.utcnow() + datetime.timedelta(days=3650)
            ).add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            ).add_extension(
                x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                              data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                              encipher_only=False, decipher_only=False), critical=True
            ).sign(private_key, hashes.SHA256())

            with open(cls.CA_KEY_PATH, "wb") as f:
                f.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            with open(cls.CA_CERT_PATH, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            
        with open(cls.CA_KEY_PATH, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(cls.CA_CERT_PATH, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        return ca_cert, ca_key

    @classmethod
    def get_cert_for_host(cls, host):
        cert_path = os.path.join(CERTS_DIR, f"{host}.crt")
        key_path = os.path.join(CERTS_DIR, f"{host}.key")
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path

        ca_cert, ca_key = cls.get_ca()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        
        try:
            ip = x509.IPAddress(socket.inet_aton(host))
            san = x509.SubjectAlternativeName([ip])
        except OSError:
            san = x509.SubjectAlternativeName([x509.DNSName(host)])

        cert = x509.CertificateBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        ).issuer_name(ca_cert.subject).public_key(private_key.public_key()).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow() - datetime.timedelta(days=1)
        ).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=365)
        ).add_extension(san, critical=False).sign(ca_key, hashes.SHA256())

        with open(key_path, "wb") as f:
            f.write(private_key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path

def nm_send_msg(msg_dict):
    try:
        msg_bytes = json.dumps(msg_dict).encode('utf-8')
        sys.stdout.buffer.write(struct.pack('@I', len(msg_bytes)))
        sys.stdout.buffer.write(msg_bytes)
        sys.stdout.buffer.flush()
    except Exception as e:
        logging.error(f"NM Send Error: {e}")

def nm_reader_thread():
    global CHROME_CONNECTED
    CHROME_CONNECTED = True
    logging.info("🌐 [Native Bridge] Chrome 扩展已连接！已开启流式传输支持。")
    try:
        while True:
            raw_len = sys.stdin.buffer.read(4)
            if len(raw_len) == 0: break
            msg_len = struct.unpack('@I', raw_len)[0]
            msg_bytes = sys.stdin.buffer.read(msg_len)
            msg = json.loads(msg_bytes.decode('utf-8'))
            
            m_type = msg.get('type')
            if m_type == 'ping':
                nm_send_msg({'type': 'pong'})
            elif m_type in ('response', 'chunk', 'end', 'error'):
                req_id = msg.get('id')
                if req_id in nm_pending_requests:
                    req_ctx = nm_pending_requests[req_id]
                    if m_type == 'response':
                        req_ctx['status'] = msg.get('status')
                        req_ctx['statusText'] = msg.get('statusText')
                        req_ctx['headers'] = msg.get('headers')
                        req_ctx['event'].set()
                    elif m_type == 'chunk':
                        b64_data = msg.get('data', '')
                        if b64_data:
                            chunk_data = base64.b64decode(b64_data)
                            try:
                                req_ctx['sock'].sendall(chunk_data)
                            except Exception as e:
                                req_ctx['error'] = str(e)
                                req_ctx['end_event'].set()
                    elif m_type == 'end':
                        req_ctx['end_event'].set()
                    elif m_type == 'error':
                        req_ctx['error'] = msg.get('error')
                        req_ctx['event'].set()
                        req_ctx['end_event'].set()
    except Exception as e:
        logging.error(f"NM Reader Aborted: {e}")
    finally:
        CHROME_CONNECTED = False
        logging.warning("🌐 [Native Bridge] Chrome 扩展已断开连接，降级为直连模式。")

def parse_http_header(sock):
    header_data = b''
    while b'\r\n\r\n' not in header_data:
        try:
            chunk = sock.recv(4096)
            if not chunk: break
            header_data += chunk
        except Exception:
            break
    if b'\r\n\r\n' not in header_data: return None, None, None, None
    
    parts = header_data.split(b'\r\n\r\n', 1)
    head = parts[0].decode('utf-8', 'ignore')
    body = parts[1] if len(parts) > 1 else b''
    
    lines = head.split('\r\n')
    req_line = lines[0].split()
    if len(req_line) < 3: return None, None, None, None
    method, url, _ = req_line
    
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip()] = v.strip()
    return method, url, headers, body

# ==================== 核心升级：Anthropic -> OpenAI 网关层 ====================
def handle_anthropic_to_openai(client_sock, body):
    try:
        try:
            body_str = body.decode('utf-8')
        except UnicodeDecodeError:
            # 兼容 Windows CMD 中文 curl 测试发来的 GBK 字节流
            body_str = body.decode('gbk', errors='replace')
            
        anthropic_req = json.loads(body_str)
        is_stream = anthropic_req.get("stream", False)
        
        # 1. 组装 OpenAI 格式 Request
        openai_req = {
            "model": LLM_MODEL,
            "messages": [],
            "stream": is_stream
        }
        if "max_tokens" in anthropic_req:
            openai_req["max_tokens"] = anthropic_req["max_tokens"]

        # 映射 System Prompt
        if "system" in anthropic_req:
            sys_content = anthropic_req["system"]
            if isinstance(sys_content, list):
                sys_content = "\n".join([item["text"] for item in sys_content if item.get("type") == "text"])
            openai_req["messages"].append({"role": "system", "content": sys_content})

        # 映射 Messages (包含文本、工具调用与多模态图像)
        for msg in anthropic_req.get("messages", []):
            role = msg["role"]
            content = msg["content"]
            
            if isinstance(content, str):
                openai_req["messages"].append({"role": role, "content": content})
            elif isinstance(content, list):
                new_content = []
                tool_calls = []
                
                for block in content:
                    if block["type"] == "text":
                        new_content.append({"type": "text", "text": block["text"]})
                    elif block["type"] == "image":
                        # 转换 Base64 图片
                        mime = block["source"]["media_type"]
                        data = block["source"]["data"]
                        new_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                    elif block["type"] == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"])
                            }
                        })
                    elif block["type"] == "tool_result":
                        # Claude 格式的 result 必须拆分作为独立消息给 OpenAI
                        res_text = block.get("content", "")
                        if isinstance(res_text, list):
                            res_text = "\n".join([p["text"] for p in res_text if p.get("type") == "text"])
                        
                        openai_req["messages"].append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": res_text
                        })

                if new_content or tool_calls:
                    new_msg = {"role": role, "content": new_content if len(new_content) > 1 else (new_content[0]["text"] if new_content else "")}
                    if tool_calls:
                        new_msg["tool_calls"] = tool_calls
                    openai_req["messages"].append(new_msg)

        # 映射 Tools 定义
        if "tools" in anthropic_req:
            openai_req["tools"] = []
            for t in anthropic_req["tools"]:
                openai_req["tools"].append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["input_schema"]
                    }
                })

        logging.info(f"🤖 [LLM Adapter] 正在请求 OpenAI 兼容接口: {LLM_API_BASE}/chat/completions")

        # 2. 发起真实请求
        req = urllib.request.Request(
            f"{LLM_API_BASE}/chat/completions",
            data=json.dumps(openai_req).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
            method="POST"
        )
        
        if not LLM_VERIFY_SSL:
            ctx = ssl._create_unverified_context()
        else:
            ctx = ssl.create_default_context()
        
        # [核心修复] 如果配了代理就用，没配就直连，彻底解除与 Chrome 的绑定以支持忽略 SSL
        if LLM_PROXY:
            proxy_handler = urllib.request.ProxyHandler({'http': LLM_PROXY, 'https': LLM_PROXY})
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))
            logging.info(f"🌐 [LLM Adapter] 正在使用指定的上游代理: {LLM_PROXY}")
        else:
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        
        with opener.open(req, timeout=120) as response:
            if not is_stream:
                # ====== 非流式响应转换 ======
                res_json = json.loads(response.read().decode('utf-8'))
                openai_msg = res_json["choices"][0]["message"]
                
                anthropic_res = {
                    "id": f"msg_{os.urandom(8).hex()}",
                    "type": "message",
                    "role": "assistant",
                    "model": LLM_MODEL,
                    "content": [],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}
                }
                
                if openai_msg.get("content"):
                    anthropic_res["content"].append({"type": "text", "text": openai_msg["content"]})
                
                if openai_msg.get("tool_calls"):
                    anthropic_res["stop_reason"] = "tool_use"
                    for tc in openai_msg["tool_calls"]:
                        anthropic_res["content"].append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"])
                        })
                
                client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
                client_sock.sendall(json.dumps(anthropic_res).encode('utf-8'))
                
            else:
                # ====== SSE 实时流响应转换 (适配 Claude Code 打字机特效) ======
                client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                
                msg_id = f"msg_{os.urandom(8).hex()}"
                
                # 初始 Start 事件
                start_evt = {
                    "type": "message_start",
                    "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": LLM_MODEL}
                }
                client_sock.sendall(f"event: message_start\ndata: {json.dumps(start_evt)}\n\n".encode('utf-8'))

                current_index = 0
                in_text_block = False
                in_tool_block = False
                finish_reason = "end_turn"

                for line in response:
                    line = line.decode('utf-8').strip()
                    if not line or not line.startswith("data: "):
                        continue
                        
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                        
                    chunk = json.loads(data_str)
                    if not chunk.get("choices"): continue
                        
                    delta = chunk["choices"][0].get("delta", {})
                    fr = chunk["choices"][0].get("finish_reason")
                    if fr == "tool_calls": finish_reason = "tool_use"

                    # 文本块处理
                    if "content" in delta and delta["content"]:
                        if not in_text_block:
                            cb_start = {"type": "content_block_start", "index": current_index, "content_block": {"type": "text", "text": ""}}
                            client_sock.sendall(f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode('utf-8'))
                            in_text_block = True
                            
                        cb_delta = {"type": "content_block_delta", "index": current_index, "delta": {"type": "text_delta", "text": delta["content"]}}
                        client_sock.sendall(f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n".encode('utf-8'))

                    # 工具调用块处理
                    if "tool_calls" in delta and delta["tool_calls"]:
                        if in_text_block:
                            cb_stop = {"type": "content_block_stop", "index": current_index}
                            client_sock.sendall(f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode('utf-8'))
                            current_index += 1
                            in_text_block = False

                        tc = delta["tool_calls"][0]
                        if tc.get("id"):
                            if in_tool_block:
                                cb_stop = {"type": "content_block_stop", "index": current_index}
                                client_sock.sendall(f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode('utf-8'))
                                current_index += 1

                            in_tool_block = True
                            cb_start = {
                                "type": "content_block_start",
                                "index": current_index,
                                "content_block": {
                                    "type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": {}
                                }
                            }
                            client_sock.sendall(f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode('utf-8'))

                        if "function" in tc and tc["function"].get("arguments"):
                            cb_delta = {
                                "type": "content_block_delta", "index": current_index,
                                "delta": {"type": "input_json_delta", "partial_json": tc["function"]["arguments"]}
                            }
                            client_sock.sendall(f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n".encode('utf-8'))

                # 扫尾结束块
                if in_text_block or in_tool_block:
                    cb_stop = {"type": "content_block_stop", "index": current_index}
                    client_sock.sendall(f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode('utf-8'))

                msg_delta = {"type": "message_delta", "delta": {"stop_reason": finish_reason, "stop_sequence": None}, "usage": {}}
                client_sock.sendall(f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n".encode('utf-8'))

                msg_stop = {"type": "message_stop"}
                client_sock.sendall(f"event: message_stop\ndata: {json.dumps(msg_stop)}\n\n".encode('utf-8'))

    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')
        error_msg = f"Upstream API Error (HTTP {e.code}): {err_body}"
        logging.error(f"❌ LLM Adapter 接口报错: {error_msg}")
        err_res = {"type": "error", "error": {"type": "api_error", "message": error_msg}}
        client_sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n\r\n" + json.dumps(err_res).encode('utf-8'))
    except urllib.error.URLError as e:
        error_msg = f"Network Connection Failed: {e.reason}"
        logging.error(f"❌ LLM Adapter 网络异常: {error_msg}")
        err_res = {"type": "error", "error": {"type": "api_error", "message": error_msg}}
        client_sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n\r\n" + json.dumps(err_res).encode('utf-8'))
    except Exception as e:
        logging.error(f"❌ LLM Adapter 内部异常: {e}")
        err_res = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
        try:
            client_sock.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n\r\n" + json.dumps(err_res).encode('utf-8'))
        except: pass
    finally:
        client_sock.close()

# ==============================================================================

def handle_local_proxy_request(client_sock):
    try:
        method, url, headers, body_prefix = parse_http_header(client_sock)
        if not method: return

        # ====================================================================
        # 🌟 核心拦截点：嗅探本地发往 Claude Code 的请求，移交 LLM Adapter 引擎
        # ====================================================================
        if LLM_ENABLED and url.endswith('/v1/messages') and method == 'POST':
            content_length = int(headers.get('Content-Length', 0))
            body = body_prefix
            while len(body) < content_length:
                chunk = client_sock.recv(min(8192, content_length - len(body)))
                if not chunk: break
                body += chunk
            
            handle_anthropic_to_openai(client_sock, body)
            return
        # ====================================================================

        target_host = headers.get('Host', '')
        if ':' in target_host: target_host = target_host.split(':')[0]

        if method == 'CONNECT':
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            cert_file, key_file = CertManager.get_cert_for_host(target_host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            
            tls_sock = context.wrap_socket(client_sock, server_side=True)
            inner_method, inner_url, inner_headers, inner_body_prefix = parse_http_header(tls_sock)
            
            if not inner_method: return
            
            # 对 HTTPS 流量同样执行内层协议拦截（防止应用强制走 HTTPS 协议访问网关）
            if LLM_ENABLED and inner_url.endswith('/v1/messages') and inner_method == 'POST':
                content_length = int(inner_headers.get('Content-Length', 0))
                body = inner_body_prefix
                while len(body) < content_length:
                    chunk = tls_sock.recv(min(8192, content_length - len(body)))
                    if not chunk: break
                    body += chunk
                handle_anthropic_to_openai(tls_sock, body)
                return

            full_url = f"https://{inner_headers.get('Host', target_host)}{inner_url}"
            process_l7_forwarding(tls_sock, inner_method, full_url, inner_headers, inner_body_prefix)
        else:
            if url.startswith('/'): url = f"http://{target_host}{url}"
            process_l7_forwarding(client_sock, method, url, headers, body_prefix)
            
    except Exception as e:
        logging.error(f"Local Proxy Handle Error: {e}")
    finally:
        try: client_sock.close()
        except: pass

def process_l7_forwarding(sock, method, url, headers, body_prefix):
    content_length = int(headers.get('Content-Length', 0))
    body = body_prefix
    while len(body) < content_length:
        body += sock.recv(8192)

    drop_req_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'host', 'accept-encoding'}
    clean_headers = {k: v for k, v in headers.items() if k.lower() not in drop_req_headers}
    
    drop_res_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'content-length', 'transfer-encoding', 'content-encoding'}

    if CHROME_CONNECTED:
        global nm_request_id_counter
        with nm_lock:
            req_id = nm_request_id_counter
            nm_request_id_counter += 1

        req_ctx = {
            'event': threading.Event(),
            'end_event': threading.Event(),
            'status': 500,
            'statusText': 'Internal Error',
            'headers': {},
            'error': None,
            'sock': sock
        }
        nm_pending_requests[req_id] = req_ctx

        b64_body = base64.b64encode(body).decode('utf-8') if body else None
        nm_send_msg({'type': 'request', 'id': req_id, 'method': method, 'url': url, 'headers': clean_headers, 'body': b64_body})

        try:
            if not req_ctx['event'].wait(timeout=30.0):
                raise Exception("Chrome Native Messaging Timeout waiting for headers")

            if req_ctx['error']:
                raise Exception(f"Chrome Fetch Error: {req_ctx['error']}")

            res_head_str = f"HTTP/1.1 {req_ctx['status']} {req_ctx['statusText']}\r\n"
            for k, v in req_ctx['headers'].items():
                if k.lower() not in drop_res_headers:
                    res_head_str += f"{k}: {v}\r\n"
            res_head_str += "Connection: close\r\n\r\n"

            sock.sendall(res_head_str.encode('utf-8'))
            req_ctx['end_event'].wait()

        except Exception as e:
            try: 
                err_msg = f"Proxy Bridge L7 Error: {e}".encode('utf-8')
                sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: " + str(len(err_msg)).encode() + b"\r\n\r\n" + err_msg)
            except: pass
            logging.error(f"Chrome Forward Error: {e}")
        finally:
            if req_id in nm_pending_requests:
                del nm_pending_requests[req_id]
    else:
        try:
            req = urllib.request.Request(url, data=body if body else None, headers=clean_headers, method=method)
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
                res_body = response.read()
                res_head_str = f"HTTP/1.1 {response.status} OK\r\n"
                for k, v in response.headers.items():
                    if k.lower() not in drop_res_headers:
                        res_head_str += f"{k}: {v}\r\n"
                res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                sock.sendall(res_head_str.encode('utf-8'))
                sock.sendall(res_body)
        except urllib.error.HTTPError as e:
            res_body = e.read()
            res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\n"
            for k, v in e.headers.items():
                if k.lower() not in drop_res_headers:
                    res_head_str += f"{k}: {v}\r\n"
            res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
            sock.sendall(res_head_str.encode('utf-8'))
            sock.sendall(res_body)
        except Exception as e:
            try:
                err_msg = f"Proxy Bridge URLLib Error: {e}".encode('utf-8')
                sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: " + str(len(err_msg)).encode() + b"\r\n\r\n" + err_msg)
            except: pass

def start_local_proxy():
    proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_sock.bind((LOCAL_PROXY_IP, LOCAL_PROXY_PORT))
    proxy_sock.listen(128)
    logging.info(f"🟢 [MITM Proxy / LLM Adapter] 本地网络引擎启动: {LOCAL_PROXY_IP}:{LOCAL_PROXY_PORT}")
    
    while True:
        try:
            client_sock, _ = proxy_sock.accept()
            threading.Thread(target=handle_local_proxy_request, args=(client_sock,), daemon=True).start()
        except Exception as e:
            logging.error(f"Proxy Accept Error: {e}")

class RC4:
    def __init__(self, key: bytes):
        self.S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + self.S[i] + key[i % len(key)]) % 256
            self.S[i], self.S[j] = self.S[j], self.S[i]
        self.i = self.j = 0

    def process(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for k in range(len(data)):
            self.i = (self.i + 1) % 256
            self.j = (self.j + self.S[self.i]) % 256
            self.S[self.i], self.S[self.j] = self.S[self.j], self.S[self.i]
            out[k] = data[k] ^ self.S[(self.S[self.i] + self.S[self.j]) % 256]
        return bytes(out)

class ClientMultiplexer:
    def __init__(self):
        self.sock = None
        self.send_queue = queue.Queue()
        self.streams = {}
        self.lock = threading.Lock()
        self.connected = False

    def send_packet(self, cmd, stream_id=0, payload=b''):
        if not self.connected: return
        header = struct.pack('!B I I', cmd, stream_id, len(payload))
        frame = struct.pack('!I', len(header) + len(payload)) + header + payload
        self.send_queue.put(frame)

    def close_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams:
                try: self.streams[stream_id].close()
                except: pass
                del self.streams[stream_id]
        self.send_packet(6, stream_id)

client_mux = ClientMultiplexer()

def pump_data(src, stream_id):
    try:
        while client_mux.connected:
            data = src.recv(8192)
            if not data: break
            client_mux.send_packet(5, stream_id, data)
    except: pass
    finally: client_mux.close_stream(stream_id)

def handle_new_tunnel_stream(stream_id, payload):
    try:
        proto_id = struct.unpack('!B', payload[:1])[0]
        offset = 1
        host_len = struct.unpack('!H', payload[offset:offset+2])[0]; offset += 2
        host = struct.unpack(f'!{host_len}s', payload[offset:offset+host_len])[0].decode('utf-8'); offset += host_len
        port = struct.unpack('!H', payload[offset:offset+2])[0]; offset += 2
        init_data_len = struct.unpack('!I', payload[offset:offset+4])[0]; offset += 4
        init_data = payload[offset:offset+init_data_len]

        target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_sock.settimeout(5.0)

        if CHROME_CONNECTED and proto_id in (1, 3):
            logging.info(f"♻️ [Loopback Routing] 隧道流量回环至 Chrome: [{host}:{port}]")
            target_sock.connect((LOCAL_PROXY_IP, LOCAL_PROXY_PORT))
            if proto_id == 1: 
                target_sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode('utf-8'))
                resp = b''
                while b'\r\n\r\n' not in resp:
                    chunk = target_sock.recv(1024)
                    if not chunk: break
                    resp += chunk
            else:
                target_sock.sendall(init_data)
        else:
            logging.info(f"⚡ [Direct Routing] 隧道任务直连 -> {host}:{port}")
            target_sock.connect((host, port))
            if init_data: target_sock.sendall(init_data)

        target_sock.settimeout(None)
        with client_mux.lock:
            client_mux.streams[stream_id] = target_sock

        client_mux.send_packet(4, stream_id, struct.pack('!B', 0))
        threading.Thread(target=pump_data, args=(target_sock, stream_id), daemon=True).start()

    except Exception as e:
        logging.error(f"❌ 隧道派发连接失败: {e}")
        client_mux.send_packet(4, stream_id, struct.pack('!B', 1))

def tunnel_worker():
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(10.0)
            sock.connect((SERVER_ADDR, SERVER_PORT))
            sock.settimeout(None)
            
            rx_key = hashlib.sha256(SECRET_KEY + b'S2C').digest()
            tx_key = hashlib.sha256(SECRET_KEY + b'C2S').digest()
            rc4_rx = RC4(rx_key)
            rc4_tx = RC4(tx_key)

            client_mux.sock = sock
            client_mux.connected = True
            logging.info("🔗 [RC4 Tunnel] 底层高并发加密多路复用隧道已连接！")

            try:
                with open(CertManager.CA_CERT_PATH, 'rb') as f:
                    ca_data = f.read()
                client_mux.send_packet(7, 0, ca_data)
            except Exception as e:
                logging.warning(f"推送 CA 证书失败: {e}")

            last_recv_time = time.time()
            
            def heartbeat_daemon():
                nonlocal last_recv_time
                while client_mux.connected:
                    time.sleep(10)
                    if not client_mux.connected: break
                    try:
                        client_mux.send_packet(1)
                    except: pass
                    
                    if time.time() - last_recv_time > 30:
                        logging.error("💔 心跳响应超时！判定网络物理中断，强制重连...")
                        client_mux.connected = False
                        try: client_mux.sock.close() 
                        except: pass
                        break

            threading.Thread(target=heartbeat_daemon, daemon=True).start()

            def writer():
                while client_mux.connected:
                    try:
                        frame = client_mux.send_queue.get(timeout=1.0)
                        client_mux.sock.sendall(rc4_tx.process(frame))
                    except queue.Empty: pass
                    except: client_mux.connected = False
            threading.Thread(target=writer, daemon=True).start()

            def recv_enc(n):
                d = bytearray()
                while len(d) < n:
                    p = sock.recv(n - len(d))
                    if not p: return None
                    d.extend(p)
                return rc4_rx.process(bytes(d))

            while client_mux.connected:
                len_b = recv_enc(4)
                if not len_b: raise Exception("对端断开或网络物理中断")
                
                packet = recv_enc(struct.unpack('!I', len_b)[0])
                if not packet: raise Exception("读取数据包错误")
                
                last_recv_time = time.time()
                
                cmd, stream_id, _ = struct.unpack('!B I I', packet[:9])
                payload = packet[9:]

                if cmd == 2:
                    continue 
                elif cmd == 3: 
                    threading.Thread(target=handle_new_tunnel_stream, args=(stream_id, payload), daemon=True).start()
                elif cmd == 5:
                    with client_mux.lock:
                        if stream_id in client_mux.streams:
                            try: client_mux.streams[stream_id].sendall(payload)
                            except: client_mux.close_stream(stream_id)
                elif cmd == 6:
                    client_mux.close_stream(stream_id)

        except Exception as e:
            logging.error(f"⚠️ 隧道连接异常 (准备重连): {e}")
        finally:
            client_mux.connected = False
            try:
                if client_mux.sock: client_mux.sock.close()
            except: pass
            time.sleep(3)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
        CertManager.get_ca()
        sys.exit(0)

    logging.info("=" * 60)
    logging.info("🚀 Super Bridge: L4 Multiplex Tunnel + LLM Adapter")
    logging.info("=" * 60)

    threading.Thread(target=start_local_proxy, daemon=True).start()
    threading.Thread(target=tunnel_worker, daemon=True).start()
    nm_reader_thread()