"""
Proxy Bridge v3.0 — shared utilities module.
Configuration, logging, and NM queue infrastructure.
"""
import json
import logging
import os
import queue
import sys
import threading

# ===========================================================================
# 1. Windows stdout/stderr setup (critical for Native Messaging)
# ===========================================================================
if sys.platform == 'win32':
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

# Redirect stdout to stderr so stray print() doesn't corrupt NM protocol
original_stdout_buffer = sys.stdout.buffer
sys.stdout = sys.stderr

# ===========================================================================
# 2. Logging setup
# ===========================================================================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'super_bridge.log')

logger = logging.getLogger('proxy_bridge')
logger.setLevel(logging.DEBUG)

# File handler
fh = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
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
# 3. Configuration
# ===========================================================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

_DEFAULT_CONFIG = {
    'local_proxy_ip': '0.0.0.0',
    'local_proxy_port': 60130,
}


def _load_config():
    """Load settings.json, falling back to defaults."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
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
