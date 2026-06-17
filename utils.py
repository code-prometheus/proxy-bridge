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
        "routing": {
            "auto_learn_enable": True,
            "direct_connect_timeout": 3.0,
            "proxy_domain_list": [
                "*.github.com", "*.github.io",
                "*.googleapis.com", "*.google.com",
                "*.golang.org",
                "*.docker.io", "*.docker.com",
                "*.npmjs.com",
                "*.openai.com", "*.anthropic.com",
                "*.huggingface.co"
            ]
        },
        "active_llm": "默认本地大模型",
        "llms": {
            "默认本地大模型": {
                "model_name": "default-model",
                "api_key": "sk-dummy",
                "verify_ssl": False,
                "base_url": "http://127.0.0.1:8000/v1"
            }
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
except Exception as e:
    logging.error(f"❌ 致命错误：你的 settings.json 格式损坏了！")
    logging.error(f"🔧 错误详情：{e}")
    sys.exit(1)

# ==================== 智能分流与自动学习配置 (带缓存防频繁IO) ====================
_domain_config_cache = None
_domain_config_mtime = 0
_routing_lock = threading.Lock()

def get_domain_config():
    global _domain_config_cache, _domain_config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if _domain_config_cache is None or mtime > _domain_config_mtime:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            routing = cfg.get('routing', {})
            _domain_config_cache = {
                'auto_learn_enable': routing.get('auto_learn_enable', True),
                'direct_connect_timeout': float(routing.get('direct_connect_timeout', 3.0)),
                'proxy_domain_list': routing.get('proxy_domain_list', [
                    "*.github.com", "*.github.io", "*.googleapis.com", "*.google.com",
                    "*.golang.org", "*.docker.io", "*.docker.com", "*.npmjs.com",
                    "*.openai.com", "*.anthropic.com", "*.huggingface.co"
                ])
            }
            _domain_config_mtime = mtime
    except Exception:
        if _domain_config_cache is None:
            _domain_config_cache = {'auto_learn_enable': True, 'direct_connect_timeout': 3.0, 'proxy_domain_list': []}
    return _domain_config_cache

def match_domain(domain, domain_list):
    if not domain: return False
    domain_lower = domain.lower()
    for pattern in domain_list:
        pattern_lower = pattern.lower()
        if pattern_lower.startswith("*."):
            suffix = pattern_lower[1:]
            if domain_lower.endswith(suffix) or domain_lower == suffix[1:]:
                return True
        elif domain_lower == pattern_lower:
            return True
    return False

def extract_main_domain(domain):
    """提取二级主域，避免无意义的一级泛滥"""
    parts = domain.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return domain
    if len(parts) >= 2:
        return "*." + ".".join(parts[-2:])
    return domain

def add_to_proxy_list(domain):
    pattern = extract_main_domain(domain)
    with _routing_lock:
        try:
            with open(CONFIG_PATH, 'r+', encoding='utf-8') as f:
                cfg = json.load(f)
                
                if 'routing' not in cfg:
                    cfg['routing'] = {
                        'auto_learn_enable': True, 
                        'direct_connect_timeout': 3.0, 
                        'proxy_domain_list': [
                            "*.github.com", "*.github.io", "*.googleapis.com", "*.google.com",
                            "*.golang.org", "*.docker.io", "*.docker.com", "*.npmjs.com",
                            "*.openai.com", "*.anthropic.com", "*.huggingface.co"
                        ]
                    }
                
                current_list = cfg['routing'].get('proxy_domain_list', [])
                
                if not match_domain(domain, current_list) and pattern not in current_list:
                    current_list.append(pattern)
                    cfg['routing']['proxy_domain_list'] = current_list
                    
                    # 回写持久化
                    f.seek(0)
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                    f.truncate()
                    
                    # 主动失效当前缓存，以便下个请求立即拉取新配置
                    global _domain_config_mtime
                    _domain_config_mtime = 0 
                    
                    logging.info(f"🌐 [Auto-Learn] 直连失败，已将 {pattern} 自动加入代理名单并保存配置。")
        except Exception as e:
            logging.error(f"❌ 自动学习写入配置失败: {e}")

# ==================== 动态读取配置文件机制 ====================
# 运用 PEP 562 模块级 __getattr__ 魔法
# 确保每次外部调用 utils.ACTIVE_LLM_KEY 或 utils.LLMS_CONFIG 时，均会实时从硬盘读取最新资源池
def __getattr__(name):
    if name in ('ACTIVE_LLM_KEY', 'LLMS_CONFIG'):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if name == 'ACTIVE_LLM_KEY':
                return cfg.get('active_llm', '')
            if name == 'LLMS_CONFIG':
                return cfg.get('llms', {})
        except Exception as e:
            logging.error(f"动态读取配置失败: {e}")
            if name == 'ACTIVE_LLM_KEY': return ''
            if name == 'LLMS_CONFIG': return {}
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def update_active_llm(new_model):
    try:
        with open(CONFIG_PATH, 'r+', encoding='utf-8') as f:
            cfg = json.load(f)
            cfg['active_llm'] = new_model
            f.seek(0)
            json.dump(cfg, f, indent=4, ensure_ascii=False)
            f.truncate()
    except Exception as e:
        logging.error(f"❌ 更新 active_llm 失败: {e}")


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
