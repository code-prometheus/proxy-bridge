"""
Proxy Bridge v2.0 — Local HTTP/HTTPS Proxy powered by Chrome network stack.
Entry point: starts the proxy server and Native Messaging bridge.

Usage:
    python entry.py              Start proxy server
    python entry.py --init-ca    Generate CA certificate
    python entry.py --install-ca Install CA to Windows trust store (run as Admin)
"""
import logging
import sys
import threading
import time
import traceback

import utils
from local_proxy import start_proxy_server, start_native_bridge


def main():
    try:
        # --init-ca: Generate root CA certificate
        if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
            utils.CertManager.get_ca()
            print('SUCCESS: CA certificate generated at ~/.proxy-bridge-ca/')
            print("Run 'python entry.py --install-ca' (as Administrator) to trust it system-wide.")
            sys.exit(0)

        # --install-ca: Install CA to Windows system trust store
        if len(sys.argv) > 1 and sys.argv[1] == '--install-ca':
            success, msg = utils.CertManager.install_ca_to_system()
            if success:
                print(f'SUCCESS: {msg}')
                print('CA certificate is now trusted by all applications.')
            else:
                print(f'FAILED: {msg}')
                print('Tip: Right-click → Run as Administrator for system-wide trust.')
            sys.exit(0 if success else 1)

        # Normal start
        logging.info('=' * 50)
        logging.info('Proxy Bridge v2.0 — Local HTTP/HTTPS Proxy')
        logging.info(f'Proxy: {utils.LOCAL_PROXY_IP}:{utils.LOCAL_PROXY_PORT}')
        logging.info('Powered by Chrome network stack (Native Messaging)')
        logging.info('=' * 50)

        # Start proxy server in daemon thread
        threading.Thread(target=start_proxy_server, daemon=True).start()

        # Native Messaging bridge (runs on main thread, blocking)
        start_native_bridge()

        # Keep alive (should not reach here normally since NM bridge blocks)
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info('Shutting down gracefully...')
        sys.exit(0)
    except Exception as e:
        logging.error(f'Fatal error: {e}')
        traceback.print_exc()
        time.sleep(5)
        sys.exit(1)


if __name__ == '__main__':
    main()
