import base64
import json
import logging
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import utils
from anthropic_proxy import handle_anthropic_api

# ================= 核心并发引擎 =================
llm_executor = ThreadPoolExecutor(max_workers=20)
tcp_executor = ThreadPoolExecutor(max_workers=200)

def tcp_pump(src, dst):
    """通用双向全双工TCP数据泵 (用于直连透传)"""
    try:
        while True:
            data = src.recv(8192)
            if not data: break
            dst.sendall(data)
    except:
        pass
    finally:
        try: src.close()
        except: pass
        try: dst.close()
        except: pass


# ================= 抵抗 Windows 管道碎片化的绝对安全读取器 =================
def read_exactly(stream, num_bytes):
    """确保在 Windows 命名管道碎片化时，严格阻塞读取足量字节，防误判 EOF"""
    data = b''
    while len(data) < num_bytes:
        chunk = stream.read(num_bytes - len(data))
        if not chunk:
            break
        data += chunk
    return data


# ================= Native Messaging 绝对单向安全写线程 =================
def native_writer_thread():
    while True:
        msg = utils.nm_send_queue.get()
        try:
            encoded_msg = json.dumps(msg).encode('utf-8')
            # 仅在此专属单例线程调用系统原生 stdout 接口
            utils.original_stdout_buffer.write(struct.pack('@I', len(encoded_msg)))
            utils.original_stdout_buffer.write(encoded_msg)
            utils.original_stdout_buffer.flush()
        except Exception as e:
            logging.error(f"Native Writer 异常: {e}")
        finally:
            utils.utils.nm_send_queue.task_done() if hasattr(utils, 'utils') else utils.nm_send_queue.task_done()


# ================= Native Messaging 绝对单向安全读线程 =================
def native_reader_thread():
    utils.CHROME_CONNECTED = True
    logging.info("🌐 [Native Bridge] Chrome 扩展已连接！已开启流式传输支持。")
    try:
        while True:
            if sys.stdin is None: break
            # 修复：使用 read_exactly 防止 Windows Pipe 碎块化导致提前误报断开
            raw_len = read_exactly(sys.stdin.buffer, 4)
            if not raw_len or len(raw_len) < 4:
                logging.warning("🌐 [Native Bridge] 收到 EOF，Chrome 已主动关闭底层管道。")
                break

            msg_len = struct.unpack('@I', raw_len)[0]
            msg_bytes = read_exactly(sys.stdin.buffer, msg_len)
            if not msg_bytes or len(msg_bytes) < msg_len:
                break

            msg = json.loads(msg_bytes.decode('utf-8'))

            m_type = msg.get('type')
            if m_type in ('ping', 'PING'):
                utils.nm_send_queue.put({'type': 'pong'})
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
                        except:
                            pass
                        req_ctx['end_event'].set()
                    elif m_type == 'error':
                        req_ctx['error'] = msg.get('error')
                        req_ctx['event'].set()
                        req_ctx['end_event'].set()
    except Exception as e:
        logging.error(f"NM Reader 意外终止: {e}")
    finally:
        utils.CHROME_CONNECTED = False
        logging.warning("🌐 [Native Bridge] Chrome 扩展已断开连接，现已降级为直连模式。")
        # 🛡️ 核心防僵尸进程自毁机制：如果是 Chrome 后台静默唤起的，管道断开后必须自杀！
        if sys.stdin and not sys.stdin.isatty():
            # 优化了提示日志，缓解恐慌感，声明这是配合 Chrome MV3 机制正常的操作
            logging.warning(
                "🛑 致命防护: 管道断裂，执行自我清理退位！(注：此为正常设计，Chrome 断网恢复后系统会自动拉起新进程)")
            os._exit(0)


def start_native_bridge():
    threading.Thread(target=native_writer_thread, daemon=True).start()
    threading.Thread(target=native_reader_thread, daemon=True).start()


def process_l7_forwarding(sock, method, url, headers, body_prefix):
    target_host_clean = utils.get_header(headers, 'Host').split(':')[0]

    if target_host_clean in ('127.0.0.1', 'localhost', utils.LOCAL_PROXY_IP) and method in ('GET', 'HEAD', 'OPTIONS'):
        if '/proxy-api/' not in url and '/v1/' not in url:
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            sock.close()
            return True

    if utils.get_header(headers, 'Expect').lower() == '100-continue':
        try:
            sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        except:
            pass

    if method == 'OPTIONS' and '/proxy-api/models' in url:
        sock.sendall(
            b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n\r\n")
        sock.close()
        return True
    if method == 'GET' and '/proxy-api/models' in url:
        body_out = json.dumps({"active_llm": utils.ACTIVE_LLM_KEY, "models": list(utils.LLMS_CONFIG.keys())}).encode(
            'utf-8')
        sock.sendall(
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(body_out)}\r\nConnection: close\r\n\r\n".encode(
                'utf-8') + body_out)
        sock.close()
        return True
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
            else:
                res = b'{"status": "error", "msg": "model not found"}'
        except Exception as e:
            res = json.dumps({"status": "error", "msg": str(e)}).encode('utf-8')
        sock.sendall(
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(res)}\r\nConnection: close\r\n\r\n".encode(
                'utf-8') + res)
        sock.close()
        return True

    # 💡 彻底并发解耦：LLM 对接抛入独立执行器，不阻碍主 TCP 调度
    if method == 'POST' and ('/v1/messages' in url):
        llm_executor.submit(handle_anthropic_api, sock, method, url, headers, body_prefix)
        return True  # 表示已移交后台异步处理完毕，外层勿杀 socket

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
            try:
                chunk_size = int(body[:idx].split(b';')[0].strip(), 16)
            except:
                break
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
            decoded.extend(body[idx + 2: idx + 2 + chunk_size])
            body = body[required_len:]
        body = bytes(decoded)
    else:
        content_length = int(utils.get_header(headers, 'Content-Length', '0'))
        while len(body) < content_length:
            chunk = sock.recv(8192)
            if not chunk: break
            body += chunk

    drop_req_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'host', 'accept-encoding',
                        'transfer-encoding'}
    clean_headers = {k: v for k, v in headers.items() if k.lower() not in drop_req_headers}
    if is_chunked: clean_headers['Content-Length'] = str(len(body))
    drop_res_headers = {'connection', 'proxy-connection', 'keep-alive', 'upgrade', 'content-length',
                        'transfer-encoding', 'content-encoding'}

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

        utils.nm_send_msg(
            {'type': 'request_start', 'id': req_id, 'method': method, 'url': url, 'headers': clean_headers})

        if body:
            chunk_size = 512 * 1024
            for i in range(0, len(body), chunk_size):
                b64_chunk = base64.b64encode(body[i:i + chunk_size]).decode('utf-8')
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
            try:
                sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except:
                pass
        finally:
            req_ctx['headers_sent'].set()

        try:
            req_ctx['end_event'].wait()
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

    return False  # 返回 False 代表同步执行结束，允许直接释放 socket


def handle_local_proxy_request(client_sock):
    handed_off = False
    try:
        method, url, headers, body_prefix = utils.parse_http_header(client_sock)
        if not method: return

        target_host_raw = utils.get_header(headers, 'Host', '')
        if not target_host_raw and method == 'CONNECT': target_host_raw = url
        target_host = target_host_raw.split(':')[0]
        
        try:
            port = int(target_host_raw.split(':')[1]) if ':' in target_host_raw else (443 if method == 'CONNECT' else 80)
        except:
            port = 443 if method == 'CONNECT' else 80

        # ================= 新增：域名分流逻辑 =================
        domain_config = utils.get_domain_config()
        is_local_or_api = target_host in ('127.0.0.1', 'localhost', utils.LOCAL_PROXY_IP)
        
        # 判断是否强制走代理 (本地 LLM 以及在名单中的站点必须走代理)
        use_proxy = is_local_or_api or utils.match_domain(target_host, domain_config['proxy_domain_list'])

        if not use_proxy:
            # 尝试直连透传
            direct_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            direct_sock.settimeout(domain_config['direct_connect_timeout'])
            try:
                direct_sock.connect((target_host, port))
                direct_sock.settimeout(None)
                logging.info(f"🚀 [分流直连] {target_host}:{port}")
                
                if method == 'CONNECT':
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    threading.Thread(target=tcp_pump, args=(client_sock, direct_sock), daemon=True).start()
                    threading.Thread(target=tcp_pump, args=(direct_sock, client_sock), daemon=True).start()
                    handed_off = True
                    return
                else:
                    # HTTP 协议转发，重构相对路径请求头 (将绝对URL转化为相对URL)
                    path = url
                    if url.lower().startswith("http://"):
                        idx = url.find('/', 7)
                        path = url[idx:] if idx != -1 else '/'
                    elif url.lower().startswith("https://"):
                        idx = url.find('/', 8)
                        path = url[idx:] if idx != -1 else '/'
                    if not path.startswith('/'): path = '/' + path
                    
                    req_line = f"{method} {path} HTTP/1.1\r\n".encode('utf-8')
                    
                    headers_bytes = b""
                    for k, v in headers.items():
                        if k.lower() not in ('proxy-connection',):
                            headers_bytes += f"{k}: {v}\r\n".encode('utf-8')
                    headers_bytes += b"\r\n"
                    
                    direct_sock.sendall(req_line + headers_bytes + body_prefix)
                    threading.Thread(target=tcp_pump, args=(client_sock, direct_sock), daemon=True).start()
                    threading.Thread(target=tcp_pump, args=(direct_sock, client_sock), daemon=True).start()
                    handed_off = True
                    return
                    
            except Exception as e:
                # 直连失败，捕获异常，将自动学习写入黑名单配置表，并向下降级走代理
                logging.warning(f"⚠️ [直连失败] {target_host}:{port} ({e})，正在降级走代理...")
                direct_sock.close()
                if domain_config['auto_learn_enable']:
                    utils.add_to_proxy_list(target_host)
        else:
            if not is_local_or_api:
                logging.info(f"🛡️ [规则代理] {target_host}:{port}")
        # ================= 分流逻辑结束 =================

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
            handed_off = process_l7_forwarding(tls_sock, inner_method, full_url, inner_headers, inner_body_prefix)
        else:
            if url.startswith('/'): url = f"http://{target_host}{url}"
            handed_off = process_l7_forwarding(client_sock, method, url, headers, body_prefix)
    except Exception as e:
        pass
    finally:
        if not handed_off:
            try:
                client_sock.close()
            except:
                pass


def start_local_proxy():
    try:
        proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 🛡️ 核心修复：Windows 系统绝对不可使用 SO_REUSEADDR！
        # 否则旧的僵尸进程不断劫持 60130，导致流量分配异常，Chrome 完全瘫痪！
        if sys.platform != "win32":
            proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # 带有退避容错的平滑重试绑定机制 (防止刚断网时旧端口没被内核回收)
        bound = False
        max_retries = 15
        for attempt in range(max_retries):
            try:
                proxy_sock.bind((utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT))
                bound = True
                break
            except Exception as e:
                logging.warning(
                    f"⚠️ 端口 {utils.LOCAL_PROXY_PORT} 绑定失败 (尝试 {attempt + 1}/{max_retries})，可能处于 TIME_WAIT 状态，等待系统回收: {e}")
                time.sleep(2)

        if not bound:
            raise Exception(f"端口 {utils.LOCAL_PROXY_PORT} 被顽固占用，超过最大重试次数。")

        proxy_sock.listen(128)
        logging.info(
            f"🟢 [MITM Proxy] 本地 HTTPS 中间人代理 / LLM API 控制端启动: {utils.LOCAL_PROXY_IP}:{utils.LOCAL_PROXY_PORT}")

        while True:
            try:
                client_sock, _ = proxy_sock.accept()
                # 设定合理超时，防止各种无厘头僵尸网络塞满系统文件描述符
                client_sock.settimeout(120.0)
                tcp_executor.submit(handle_local_proxy_request, client_sock)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"❌ 代理绑定彻底失败: {e}")
        logging.warning("⚠️ 降级运行：将跳过本地监听，仅保留 Chrome Native Bridge 通信功能。")
        # 就算端口被占用，也要维持守护循环不退出，专职服务 Chrome 桥接即可。
        while True:
            time.sleep(3600)
