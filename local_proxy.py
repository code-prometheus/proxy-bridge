# 完整实现本地 HTTP/SOCKS5 代理服务，流量转发、SSL处理功能
import logging
import socket
import struct
import threading
import json
import base64
import urllib.request
import urllib.error
import ssl
import sys
import os

import utils
from claude_anthropic_proxy import handle_anthropic_api

def nm_reader_thread():
    utils.CHROME_CONNECTED = True
    logging.info("🌐 [Native Bridge] Chrome 扩展已连接！已开启流式传输支持。")
    try:
        while True:
            if sys.stdin is None: break
            raw_len = sys.stdin.buffer.read(4)
            if len(raw_len) < 4: break
            msg_len = struct.unpack('@I', raw_len)[0]
            msg_bytes = sys.stdin.buffer.read(msg_len)
            msg = json.loads(msg_bytes.decode('utf-8'))
            m_type = msg.get('type')
            if m_type == 'ping':
                utils.nm_send_msg({'type': 'pong'})
            elif m_type in ('response', 'chunk', 'end', 'error'):
                req_id = msg.get('id')
                if req_id in utils.nm_pending_requests:
                    req_ctx = utils.nm_pending_requests[req_id]
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
                                if 'headers_sent' in req_ctx: req_ctx['headers_sent'].wait()
                                if req_ctx.get('use_chunked_response'):
                                    chunk_header = f"{len(chunk_data):X}\r\n".encode('utf-8')
                                    req_ctx['sock'].sendall(chunk_header + chunk_data + b"\r\n")
                                else:
                                    req_ctx['sock'].sendall(chunk_data)
                            except Exception as e:
                                req_ctx['error'] = str(e)
                                req_ctx['end_event'].set()
                    elif m_type == 'end':
                        try:
                            if 'headers_sent' in req_ctx: req_ctx['headers_sent'].wait()
                            if req_ctx.get('use_chunked_response'):
                                req_ctx['sock'].sendall(b"0\r\n\r\n")
                        except: pass
                        req_ctx['end_event'].set()
                    elif m_type == 'error':
                        req_ctx['error'] = msg.get('error')
                        req_ctx['event'].set()
                        req_ctx['end_event'].set()
    except Exception as e:
        logging.error(f"NM Reader 意外终止 (若您未通过 Chrome 运行，可无视此报错): {e}")
    finally:
        utils.CHROME_CONNECTED = False
        logging.warning("🌐 [Native Bridge] Chrome 扩展已断开连接，现已降级为直连模式。")

def process_l7_forwarding(sock, method, url, headers, body_prefix):
    target_host_clean = utils.get_header(headers, 'Host').split(':')[0]

    if target_host_clean in ('127.0.0.1', 'localhost', utils.LOCAL_PROXY_IP) and method in ('GET', 'HEAD', 'OPTIONS'):
        if '/proxy-api/' not in url and '/v1/' not in url:
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            sock.close()
            return

    if utils.get_header(headers, 'Expect').lower() == '100-continue':
        try: sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        except: pass

    if method == 'OPTIONS' and '/proxy-api/models' in url:
        sock.sendall(b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n")
        sock.close()
        return
    if method == 'GET' and '/proxy-api/models' in url:
        body_out = json.dumps({"active_llm": utils.ACTIVE_LLM_KEY, "models": list(utils.LLMS_CONFIG.keys())}).encode('utf-8')
        sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode('utf-8') + body_out)
        sock.close()
        return
    if method == 'POST' and '/proxy-api/models' in url:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        body = body_prefix
        while len(body) < content_length: body += sock.recv(8192)
        try:
            req = json.loads(body.decode('utf-8'))
            new_model = req.get("active_llm")
            if new_model in utils.LLMS_CONFIG:
                utils.update_active_llm(new_model)
                res = b'{"status": "success"}'
            else: res = b'{"status": "error", "msg": "model not found"}'
        except Exception as e:
            res = json.dumps({"status": "error", "msg": str(e)}).encode('utf-8')
        sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(res)}\r\nConnection: close\r\n\r\n".encode('utf-8') + res)
        sock.close()
        return

    if method == 'POST' and ('/v1/messages' in url):
        handle_anthropic_api(sock, method, url, headers, body_prefix)
        return

    is_chunked = utils.get_header(headers, 'Transfer-Encoding').lower() == 'chunked'
    body = body_prefix
    if is_chunked:
        decoded = bytearray()
        while True:
            idx = body.find(b'\r\n')
            if idx == -1:
                chunk = sock.recv(8192)
                if not chunk: break
                body += chunk
                continue
            try: chunk_size = int(body[:idx].split(b';')[0].strip(), 16)
            except: break
            if chunk_size == 0:
                while b'\r\n\r\n' not in body:
                    chunk = sock.recv(8192)
                    if not chunk: break
                    body += chunk
                break
            required_len = idx + 2 + chunk_size + 2
            while len(body) < required_len:
                chunk = sock.recv(8192)
                if not chunk: break
                body += chunk
            decoded.extend(body[idx+2 : idx+2+chunk_size])
            body = body[required_len:]
        body = bytes(decoded)
    else:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        while len(body) < content_length:
            chunk = sock.recv(8192)
            if not chunk: break
            body += chunk

    drop_req_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'host', 'accept-encoding', 'transfer-encoding'}
    clean_headers = {k: v for k, v in headers.items() if k.lower() not in drop_req_headers}
    if is_chunked: clean_headers['Content-Length'] = str(len(body))
    drop_res_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'content-length', 'transfer-encoding', 'content-encoding'}

    if utils.CHROME_CONNECTED:
        with utils.nm_lock:
            req_id = utils.nm_request_id_counter
            utils.nm_request_id_counter += 1

        req_ctx = {
            'event': threading.Event(), 'end_event': threading.Event(), 'headers_sent': threading.Event(),
            'status': 500, 'statusText': 'Internal Error', 
            'headers': {}, 'error': None, 'sock': sock, 'use_chunked_response': True 
        }
        utils.nm_pending_requests[req_id] = req_ctx
        
        utils.nm_send_msg({'type': 'request_start', 'id': req_id, 'method': method, 'url': url, 'headers': clean_headers})
        
        if body:
            chunk_size = 512 * 1024 
            for i in range(0, len(body), chunk_size):
                b64_chunk = base64.b64encode(body[i:i+chunk_size]).decode('utf-8')
                utils.nm_send_msg({'type': 'request_chunk', 'id': req_id, 'data': b64_chunk})
                
        utils.nm_send_msg({'type': 'request_end', 'id': req_id})

        try:
            if not req_ctx['event'].wait(timeout=180.0): raise Exception("Chrome Native Messaging Timeout")
            if req_ctx['error']: raise Exception(f"Chrome Fetch Error: {req_ctx['error']}")

            res_head_str = f"HTTP/1.1 {req_ctx['status']} {req_ctx['statusText']}\r\n"
            has_auth = False
            for k, v in req_ctx['headers'].items():
                if k.lower() not in drop_res_headers: res_head_str += f"{k}: {v}\r\n"
                if k.lower() == 'www-authenticate': has_auth = True
            
            res_head_str += "Transfer-Encoding: chunked\r\n"
            if req_ctx['status'] == 401 and not has_auth:
                res_head_str += 'WWW-Authenticate: Basic realm="Proxy Bridge"\r\n'
            res_head_str += "Connection: close\r\n\r\n"
            
            sock.sendall(res_head_str.encode('utf-8'))
        except Exception as e:
            try: sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except: pass
        finally:
            req_ctx['headers_sent'].set()

        try: req_ctx['end_event'].wait()
        finally:
            if req_id in utils.nm_pending_requests: del utils.nm_pending_requests[req_id]
    else:
        try:
            req = urllib.request.Request(url, data=body if body else None, headers=clean_headers, method=method)
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as response:
                res_body = response.read()
                res_head_str = f"HTTP/1.1 {response.status} OK\r\n"
                for k, v in response.headers.items():
                    if k.lower() not in drop_res_headers: res_head_str += f"{k}: {v}\r\n"
                res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
                sock.sendall(res_head_str.encode('utf-8') + res_body)
        except urllib.error.HTTPError as e:
            res_body = e.read()
            res_head_str = f"HTTP/1.1 {e.code} {e.reason}\r\n"
            for k, v in e.headers.items():
                if k.lower() not in drop_res_headers: res_head_str += f"{k}: {v}\r\n"
            res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
            sock.sendall(res_head_str.encode('utf-8') + res_body)
        except Exception:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

def handle_local_proxy_request(client_sock):
    try:
        method, url, headers, body_prefix = utils.parse_http_header(client_sock)
        if not method: return

        target_host = utils.get_header(headers, 'Host', '')
        if not target_host and method == 'CONNECT': target_host = url
        target_host = target_host.split(':')[0]

        if method == 'CONNECT':
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            cert_file, key_file = utils.CertManager.get_cert_for_host(target_host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            tls_sock = context.wrap_socket(client_sock, server_side=True)
            inner_method, inner_url, inner_headers, inner_body_prefix = utils.parse_http_header(tls_sock)
            if not inner_method: return
            
            inner_host = utils.get_header(inner_headers, 'Host', target_host).split(':')[0]
            full_url = f"https://{inner_host}{inner_url}"
            process_l7_forwarding(tls_sock, inner_method, full_url, inner_headers, inner_body_prefix)
        else:
            if url.startswith('/'): url = f"http://{target_host}{url}"
            process_l7_forwarding(client_sock, method, url, headers, body_prefix)
    except Exception as e:
        pass
    finally:
        try: client_sock.close()
        except: pass

def start_local_proxy():
    try:
        proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        proxy_sock.bind((utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT))
        proxy_sock.listen(128)
        logging.info(f"🟢 [MITM Proxy] 本地 HTTPS 中间人代理 / LLM API 控制端启动: {utils.LOCAL_PROXY_IP}:{utils.LOCAL_PROXY_PORT}")
        while True:
            try:
                client_sock, _ = proxy_sock.accept()
                threading.Thread(target=handle_local_proxy_request, args=(client_sock,), daemon=True).start()
            except Exception: pass
    except Exception as e:
        logging.error(f"❌ 代理启动失败 (端口是否被占用？): {e}")
        os._exit(1)