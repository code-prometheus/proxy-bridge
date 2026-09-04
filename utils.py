"""
Proxy Bridge — shared utilities module.
Local HTTP/HTTPS proxy powered by Chrome's network stack via Native Messaging.
"""

# ===========================================================================
# 1. Windows stdout/stderr setup (critical for Native Messaging)
# ===========================================================================
import sys
import os

if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

original_stdout_buffer = sys.stdout.buffer
sys.stdout = sys.stderr  # Prevent stray print() from corrupting NM protocol


# ===========================================================================
# 2. Imports
# ===========================================================================
import json
import logging
import queue
import threading
import datetime
import subprocess
import socket as socket_module

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import ExtensionNotFound


# ===========================================================================
# 3. Logging setup
# ===========================================================================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'super_bridge.log')

logger = logging.getLogger('proxy_bridge')
logger.setLevel(logging.DEBUG)

# File handler
fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(fh)

# Stderr handler (only if stdin is a tty — i.e. not launched via NM host)
if sys.stdin.isatty():
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(sh)


# ===========================================================================
# 4. Configuration
# ===========================================================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

_DEFAULT_CONFIG = {
    "local_proxy_ip": "127.0.0.1",
    "local_proxy_port": 60130,
}


def _load_config():
    """Load settings.json, falling back to defaults."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        # Support both flat structure and nested client block
        if 'client' in cfg:
            client = cfg['client']
        else:
            client = cfg
        return {
            'local_proxy_ip': client.get('local_proxy_ip', _DEFAULT_CONFIG['local_proxy_ip']),
            'local_proxy_port': client.get('local_proxy_port', _DEFAULT_CONFIG['local_proxy_port']),
        }
    except Exception:
        return dict(_DEFAULT_CONFIG)


_config = _load_config()
LOCAL_PROXY_IP = _config['local_proxy_ip']
LOCAL_PROXY_PORT = _config['local_proxy_port']


# ===========================================================================
# 5. Native Messaging queue & state
# ===========================================================================
nm_send_queue = queue.Queue()
nm_pending_requests = {}  # {req_id: {'event': Event, 'end_event': Event, 'headers_sent': Event, ...}}
nm_request_id_counter = 1
nm_lock = threading.Lock()
CHROME_CONNECTED = False


def nm_send_msg(msg_dict):
    """Enqueue a JSON-serialisable dict for delivery to Chrome via stdout."""
    nm_send_queue.put(msg_dict)


# ===========================================================================
# 6. HTTP parsing
# ===========================================================================

def parse_http_header(sock):
    """
    Read HTTP header from socket.

    Returns:
        (method, url, headers_dict, body_prefix_bytes)
        On failure returns (None, None, None, None).
    """
    data = b''
    while b'\r\n\r\n' not in data:
        try:
            chunk = sock.recv(4096)
        except Exception:
            return None, None, None, None
        if not chunk:
            return None, None, None, None
        data += chunk
        # Safety limit: 64 KB header
        if len(data) > 65536:
            return None, None, None, None

    header_end = data.find(b'\r\n\r\n')
    header_bytes = data[:header_end]
    body_prefix = data[header_end + 4:]

    header_text = header_bytes.decode('utf-8', errors='replace')
    lines = header_text.split('\r\n')

    if not lines:
        return None, None, None, None

    # Parse request line: METHOD URL HTTP/1.x
    first_line = lines[0]
    parts = first_line.split(' ', 2)
    if len(parts) < 2:
        return None, None, None, None
    method = parts[0].upper()
    url = parts[1]

    # Parse headers
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()

    return method, url, headers, body_prefix


def get_header(headers, key, default=''):
    """Case-insensitive header lookup."""
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return default


# ===========================================================================
# 7. CertManager class
# ===========================================================================

CA_DIR = os.path.join(os.path.expanduser('~'), '.proxy-bridge-ca')
CERTS_DIR = os.path.join(CA_DIR, 'certs')
os.makedirs(CERTS_DIR, exist_ok=True)


class CertManager:
    CA_CERT_PATH = os.path.join(CA_DIR, 'ca-cert.pem')
    CA_KEY_PATH = os.path.join(CA_DIR, 'ca-key.pem')

    @classmethod
    def get_ca(cls):
        """
        Generate or load the root CA certificate.

        Returns:
            (ca_cert, ca_key) — cryptography objects.
        """
        if os.path.exists(cls.CA_CERT_PATH) and os.path.exists(cls.CA_KEY_PATH):
            with open(cls.CA_CERT_PATH, 'rb') as f:
                ca_cert = x509.load_pem_x509_certificate(f.read())
            with open(cls.CA_KEY_PATH, 'rb') as f:
                ca_key = serialization.load_pem_private_key(f.read(), password=None)
            return ca_cert, ca_key

        # Generate new CA
        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, 'Proxy Bridge Local CA'),
        ])

        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365 * 10))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    key_cert_sign=True,
                    crl_sign=True,
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # Persist to disk
        with open(cls.CA_CERT_PATH, 'wb') as f:
            f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.CA_KEY_PATH, 'wb') as f:
            f.write(ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        logger.info("Generated new CA certificate and key.")
        return ca_cert, ca_key

    @classmethod
    def get_cert_for_host(cls, host):
        """
        Generate per-host certificate signed by CA for MITM.

        Args:
            host: hostname, optionally with port (host:port).

        Returns:
            (cert_path, key_path) — paths to PEM files.
        """
        # Strip port if present
        host = host.split(':')[0]

        cert_path = os.path.join(CERTS_DIR, f'{host}.crt')
        key_path = os.path.join(CERTS_DIR, f'{host}.key')

        # Return cached if available
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path

        ca_cert, ca_key = cls.get_ca()

        # Generate host key
        host_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, host),
        ])

        # Get CA SubjectKeyIdentifier for AuthorityKeyIdentifier
        try:
            ca_ski_ext = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            ca_ski = ca_ski_ext.value
        except ExtensionNotFound:
            ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())

        host_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(host)]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_cert_sign=False,
                    crl_sign=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(host_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier(
                    key_identifier=ca_ski.digest,
                    authority_cert_issuer=None,
                    authority_cert_serial_number=None,
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # Persist host cert and key
        with open(cert_path, 'wb') as f:
            f.write(host_cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, 'wb') as f:
            f.write(host_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        logger.info("Generated new certificate for host: %s", host)
        return cert_path, key_path

    @classmethod
    def get_ssl_context_for_upstream(cls, host, verify_ssl=True):
        """
        SSL context for upstream connections.

        Args:
            host: target hostname (for SNI).
            verify_ssl: if False, skip certificate verification.

        Returns:
            ssl.SSLContext.
        """
        import ssl as ssl_stdlib
        if not verify_ssl:
            return ssl_stdlib._create_unverified_context()
        ctx = ssl_stdlib.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl_stdlib.CERT_REQUIRED
        return ctx

    @classmethod
    def install_ca_to_system(cls):
        """
        Install CA to Windows root store via certutil.

        Returns:
            (success: bool, message: str)
        """
        cls.get_ca()  # ensure CA exists
        cmds = [
            ['certutil', '-addstore', '-f', 'Root', cls.CA_CERT_PATH],
            ['certutil', '-addstore', '-f', '-user', 'Root', cls.CA_CERT_PATH],
        ]
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    return True, f"Installed: {' '.join(cmd[:3])}"
            except Exception:
                pass
        return False, "Installation failed. Run as Administrator."
