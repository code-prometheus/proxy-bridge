import datetime
import json
import logging
import os
import queue
import socket
import sys
import threading

# ==================== 确保标准输入输出为二进制模式 ====================
if sys.platform == "win32":
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

# === CRITICAL: 零标准输出污染 & 绝对隔离 Native I/O ===
# 备份系统原始二进制写通道给 Native Messaging 专属使用
original_stdout_buffer = sys.stdout.buffer
# 强行将 sys.stdout 重定向至 sys.stderr，阻断任何第三方库的 print() 破坏协议
sys.stdout = sys.stderr

log_file = os.path.join(os.path.dirname(__file__), 'super_bridge.log')
handlers = [logging.FileHandler(log_file, 'a', 'utf-8')]
# 核心防杀机制：仅在用户手动双击（终端模式）时输出日志到控制台。
# 如果是被 Chrome 唤起的后台守护进程，则保持 stderr 静默，防止挤爆 Chrome 的原生错误缓冲区导致被强杀！
if sys.stdin and sys.stdin.isatty():
    handlers.append(logging.StreamHandler(sys.stderr))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', handlers=handlers)

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:
    logging.error("❌ 缺少 cryptography 库。请执行: pip install cryptography")
    sys.exit(1)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'settings.json')

if not os.path.exists(CONFIG_PATH):
    default_config = {
        "common": {"secret_key": "CHANGE_ME_TO_YOUR_TUNNEL_SECRET"},
        "client": {"server_addr": "YOUR_UBUNTU_IP_ADDRESS", "server_port": 6974, "local_proxy_ip": "127.0.0.1",
                   "local_proxy_port": 60130},
        "active_llm": "千问网页",
        "llms": {
            "千问网页": {"model_name": "qwen-max-thinking", "api_key": "sk-dummy", "verify_ssl": False,
                         "base_url": "http://127.0.0.1:5419/v1"},
            "DeepSeek网页": {"model_name": "deepseek-reasoner", "api_key": "sk-dummy", "verify_ssl": False,
                             "base_url": "http://127.0.0.1:5418/v1"},
            "DeepSeek V3满血版64K（本地）": {"base_url": "https://122.1.12.137:31004/api/v2",
                                           "model_name": "deepseek-v3-64k_zfld0z", "verify_ssl": False,
                                           "api_key": "f6483dec-b4aa-430b-bb3f-8fafdea2a456_3D61F25730ECCA33EAB04DDB4CAD00B5D9353747808BF7731A1B34C565FAF500"},
            "DeepSeek R1 满血版（本地）": {"base_url": "https://122.1.12.137:31004/api/v2",
                                         "model_name": "deepseek-r1-128k_y5hxbt", "verify_ssl": False,
                                         "api_key": "f6483dec-b4aa-430b-bb3f-8fafdea2a456_3D61F25730ECCA33EAB04DDB4CAD00B5D9353747808BF7731A1B34C565FAF500"},
            "智普5.0（本地）": {"base_url": "http://122.1.231.27:8000/v1", "model_name": "glm-5", "verify_ssl": False,
                              "api_key": "none"}
        }
    }
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        logging.info(f"✨ 首次运行，已自动生成配置模板: {CONFIG_PATH}")
    except Exception as e:
        logging.error(f"❌ 无法生成默认配置文件: {e}")
        sys.exit(1)

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
    SERVER_ADDR = config['client']['server_addr']
    SERVER_PORT = int(config['client']['server_port'])
    LOCAL_PROXY_IP = config['client'].get('local_proxy_ip', '127.0.0.1')
    LOCAL_PROXY_PORT = int(config['client'].get('local_proxy_port', 60130))
    SECRET_KEY = config['common']['secret_key'].encode('utf-8')
    ACTIVE_LLM_KEY = config.get('active_llm', '')
    LLMS_CONFIG = config.get('llms', {})
except Exception as e:
    logging.error(f"❌ 致命错误：你的 settings.json 格式损坏了！")
    logging.error(f"🔧 错误详情：{e}")
    sys.exit(1)


def update_active_llm(new_model):
    global ACTIVE_LLM_KEY
    ACTIVE_LLM_KEY = new_model
    with open(CONFIG_PATH, 'r+', encoding='utf-8') as f:
        cfg = json.load(f)
        cfg['active_llm'] = new_model
        f.seek(0);
        json.dump(cfg, f, indent=4, ensure_ascii=False);
        f.truncate()


CA_DIR = os.path.join(os.path.expanduser('~'), '.proxy-bridge-ca')
CERTS_DIR = os.path.join(CA_DIR, 'certs')
os.makedirs(CERTS_DIR, exist_ok=True)

CHROME_CONNECTED = False
# === Queue based Producer-Consumer model for Native Messaging ===
nm_send_queue = queue.Queue()
nm_pending_requests = {}
nm_request_id_counter = 1
nm_lock = threading.Lock()


class CertManager:
    CA_CERT_PATH = os.path.join(CA_DIR, 'ca-cert.pem')
    CA_KEY_PATH = os.path.join(CA_DIR, 'ca-key.pem')

    @classmethod
    def get_ca(cls):
        if not os.path.exists(cls.CA_CERT_PATH) or not os.path.exists(cls.CA_KEY_PATH):
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"Proxy Bridge Local CA")])
            cert = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(
                private_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(
                datetime.datetime.utcnow() - datetime.timedelta(days=1)).not_valid_after(
                datetime.datetime.utcnow() + datetime.timedelta(days=3650)).add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True).add_extension(
                x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                              data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                              encipher_only=False, decipher_only=False), critical=True).sign(private_key,
                                                                                             hashes.SHA256())

            with open(cls.CA_KEY_PATH, "wb") as f: f.write(
                private_key.private_bytes(encoding=serialization.Encoding.PEM,
                                          format=serialization.PrivateFormat.TraditionalOpenSSL,
                                          encryption_algorithm=serialization.NoEncryption()))
            with open(cls.CA_CERT_PATH, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))

        with open(cls.CA_KEY_PATH, "rb") as f: ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(cls.CA_CERT_PATH, "rb") as f: ca_cert = x509.load_pem_x509_certificate(f.read())
        return ca_cert, ca_key

    @classmethod
    def get_cert_for_host(cls, host):
        cert_path = os.path.join(CERTS_DIR, f"{host}.crt")
        key_path = os.path.join(CERTS_DIR, f"{host}.key")
        if os.path.exists(cert_path) and os.path.exists(key_path): return cert_path, key_path
        ca_cert, ca_key = cls.get_ca()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        try:
            ip = x509.IPAddress(socket.inet_aton(host))
            san = x509.SubjectAlternativeName([ip])
        except OSError:
            san = x509.SubjectAlternativeName([x509.DNSName(host)])
        cert = x509.CertificateBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])).issuer_name(ca_cert.subject).public_key(
            private_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(
            datetime.datetime.utcnow() - datetime.timedelta(days=1)).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=365)).add_extension(san, critical=False).sign(ca_key,
                                                                                                               hashes.SHA256())
        with open(key_path, "wb") as f:
            f.write(private_key.private_bytes(encoding=serialization.Encoding.PEM,
                                              format=serialization.PrivateFormat.TraditionalOpenSSL,
                                              encryption_algorithm=serialization.NoEncryption()))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path


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


def get_header(headers, key, default=''):
    key_lower = key.lower()
    for k, v in headers.items():
        if k.lower() == key_lower:
            return v
    return default


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


def nm_send_msg(msg_dict):
    nm_send_queue.put(msg_dict)
