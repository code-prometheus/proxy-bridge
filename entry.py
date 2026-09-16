"""
Proxy Bridge v3.0 — Local HTTP/HTTPS Proxy powered by Chrome network stack.
Entry point: starts the proxy server and Native Messaging bridge.

Usage:
    python entry.py                  Start proxy server
    python entry.py --init-ca        Generate CA certificate
    python entry.py --install-ca     Install CA to system trust store (Admin)
"""
import logging
import queue
import sys
import threading
import time
import traceback

import utils
from certs import CertManager
from proxy import start_proxy_server
from nm import start_native_bridge


def main():
    try:
        # --init-ca: Generate root CA certificate
        if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
            CertManager.get_ca()
            print('SUCCESS: CA certificate generated at ~/.proxy-bridge-ca/')
            print("Run 'python entry.py --install-ca' (as Administrator) to trust it system-wide.")
            sys.exit(0)

        # --install-ca: Install CA to system trust store
        if len(sys.argv) > 1 and sys.argv[1] == '--install-ca':
            success, msg = CertManager.install_ca_to_system()
            if success:
                print(f'SUCCESS: {msg}')
                print('CA certificate is now trusted by all applications.')
            else:
                print(f'FAILED: {msg}')
                print('Tip: Run as Administrator for system-wide trust.')
            sys.exit(0 if success else 1)

        # Normal start
        logging.info('=' * 50)
        logging.info('Proxy Bridge v3.0 — Local HTTP/HTTPS Proxy')
        logging.info('Proxy: %s:%d', utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
        logging.info('Powered by Chrome network stack (Native Messaging)')
        logging.info('=' * 50)

        # Create NM send queue
        nm_send_queue = queue.Queue()

        # Start Native Messaging bridge (reader/writer threads)
        start_native_bridge(nm_send_queue)

        # Start proxy server (blocking accept loop on main thread)
        start_proxy_server()

    except KeyboardInterrupt:
        logging.info('Shutting down gracefully...')
        sys.exit(0)
    except Exception as e:
        logging.error('Fatal error: %s', e)
        traceback.print_exc()
        time.sleep(5)
        sys.exit(1)


if __name__ == '__main__':
    main()
