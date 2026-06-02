import logging
import socket
import struct
import threading
import time
import queue
import hashlib
import configparser
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

# ==================== 核心配置 ====================
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.ini')
config = configparser.ConfigParser()
config.read(CONFIG_PATH, encoding='utf-8')

SERVER_ADDR = config.get('client', 'server_addr', fallback='122.1.17.123')
SERVER_PORT = config.getint('client', 'server_port', fallback=6974)
SECRET_KEY = config.get('common', 'secret_key', fallback='Quantitative_Trading_Tunnel_2026').encode('utf-8')

LOCAL_PROXY_PORT = 60130
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
    logging.info("🌐 [Native Bridge] Chrome 扩展已连接！已接管 L7 层网络。")
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
            elif m_type in ('response', 'chunk', 'error'):
                req_id = msg.get('id')
                if req_id in nm_pending_requests:
                    req_ctx = nm_pending_requests[req_id]
                    if m_type == 'response':
                        req_ctx['status'] = msg.get('status')
                        req_ctx['statusText'] = msg.get('statusText')
                        req_ctx['headers'] = msg.get('headers')
                        req_ctx['totalChunks'] = msg.get('totalChunks', 0)
                        if req_ctx['totalChunks'] == 0: req_ctx['event'].set()
                    elif m_type == 'chunk':
                        idx = msg.get('index')
                        b64_data = msg.get('data', '')
                        req_ctx['chunks'][idx] = base64.b64decode(b64_data) if b64_data else b''
                        req_ctx['received'] += 1
                        if req_ctx['received'] >= req_ctx['totalChunks']: req_ctx['event'].set()
                    elif m_type == 'error':
                        req_ctx['error'] = msg.get('error')
                        req_ctx['event'].set()
    except Exception as e:
        logging.error(f"NM Reader Aborted: {e}")
    finally:
        CHROME_CONNECTED = False
        logging.warning("🌐 [Native Bridge] Chrome 扩展已断开连接，降级为直连模式。")

def fetch_via_chrome(method, url, headers, body_bytes):
    global nm_request_id_counter
    with nm_lock:
        req_id = nm_request_id_counter
        nm_request_id_counter += 1
        
    req_ctx = {
        'event': threading.Event(), 'status': 500, 'statusText': 'Internal Error',
        'headers': {}, 'chunks': {}, 'received': 0, 'totalChunks': 0, 'error': None
    }
    nm_pending_requests[req_id] = req_ctx

    b64_body = base64.b64encode(body_bytes).decode('utf-8') if body_bytes else None
    nm_send_msg({'type': 'request', 'id': req_id, 'method': method, 'url': url, 'headers': headers, 'body': b64_body})

    if not req_ctx['event'].wait(timeout=60.0):
        del nm_pending_requests[req_id]
        raise Exception("Chrome Native Messaging Timeout")
    
    del nm_pending_requests[req_id]
    if req_ctx['error']: raise Exception(f"Chrome Fetch Error: {req_ctx['error']}")

    full_body = b''.join(req_ctx['chunks'][i] for i in range(req_ctx['totalChunks']) if i in req_ctx['chunks'])
    return req_ctx['status'], req_ctx['statusText'], req_ctx['headers'], full_body

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

def handle_local_proxy_request(client_sock):
    try:
        method, url, headers, body_prefix = parse_http_header(client_sock)
        if not method: return

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
            full_url = f"https://{headers.get('Host', target_host)}{inner_url}"
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
        try:
            status, text, res_headers, res_body = fetch_via_chrome(method, url, clean_headers, body)
            res_head_str = f"HTTP/1.1 {status} {text}\r\n"
            for k, v in res_headers.items():
                if k.lower() not in drop_res_headers:
                    res_head_str += f"{k}: {v}\r\n"
            res_head_str += f"Content-Length: {len(res_body)}\r\nConnection: close\r\n\r\n"
            
            sock.sendall(res_head_str.encode('utf-8'))
            sock.sendall(res_body)
        except Exception as e:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            logging.error(f"Chrome Forward Error: {e}")
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
        except Exception:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

def start_local_proxy():
    proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_sock.bind(('127.0.0.1', LOCAL_PROXY_PORT))
    proxy_sock.listen(128)
    logging.info(f"🟢 [MITM Proxy] 本地 HTTPS 中间人代理启动: 127.0.0.1:{LOCAL_PROXY_PORT}")
    
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
            target_sock.connect(('127.0.0.1', LOCAL_PROXY_PORT))
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
            sock.settimeout(10.0)
            sock.connect((SERVER_ADDR, SERVER_PORT))
            sock.settimeout(None)
            
            rx_key = hashlib.sha256(SECRET_KEY + b'S2C').digest()
            tx_key = hashlib.sha256(SECRET_KEY + b'C2S').digest()
            rc4_rx = RC4(rx_key)
            rc4_tx = RC4(tx_key)

            client_mux.sock = sock
            client_mux.connected = True
            logging.info("🔗 [RC4 Tunnel] 底层高并发加密多路复用隧道已连接 Ubuntu！")

            # ================== 【新增核心】：将本地生成的 CA 同步推送给 Ubuntu ==================
            try:
                with open(CertManager.CA_CERT_PATH, 'rb') as f:
                    ca_data = f.read()
                client_mux.send_packet(7, 0, ca_data) # 发送 CMD_SYNC_CA 指令
                logging.info("📤 已向 Ubuntu 服务端自动推送 CA 根证书...")
            except Exception as e:
                logging.warning(f"推送 CA 证书失败: {e}")

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
                if not len_b: break
                packet = recv_enc(struct.unpack('!I', len_b)[0])
                if not packet: break
                
                cmd, stream_id, _ = struct.unpack('!B I I', packet[:9])
                payload = packet[9:]

                if cmd == 3: threading.Thread(target=handle_new_tunnel_stream, args=(stream_id, payload), daemon=True).start()
                elif cmd == 5:
                    with client_mux.lock:
                        if stream_id in client_mux.streams:
                            try: client_mux.streams[stream_id].sendall(payload)
                            except: client_mux.close_stream(stream_id)
                elif cmd == 6:
                    client_mux.close_stream(stream_id)
        except Exception as e:
            logging.error(f"Tunnel Error: {e}")
        finally:
            client_mux.connected = False
            time.sleep(3)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
        CertManager.get_ca()
        sys.exit(0)

    logging.info("=" * 50)
    logging.info("🚀 Super Bridge: L4 Multiplex Tunnel + L7 Native MITM")
    logging.info("=" * 50)

    threading.Thread(target=start_local_proxy, daemon=True).start()
    threading.Thread(target=tunnel_worker, daemon=True).start()
    nm_reader_thread()