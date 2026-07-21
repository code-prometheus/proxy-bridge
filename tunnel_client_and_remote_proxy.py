"""
Super Bridge - Tunnel Client and Remote Proxy Entry Point

This module initializes and runs the tunnel client, local proxy server,
and native messaging bridge for Chrome extension integration.
"""
import logging
import sys
import threading
import time
import traceback

import utils
from local_proxy import start_local_proxy, start_native_bridge
from remote_tunnel import tunnel_worker


def main():
    """Main entry point for the tunnel client and remote proxy."""
    try:
        # Handle CA certificate initialization
        if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
            utils.CertManager.get_ca()
            sys.exit(0)

        logging.info("=" * 50)
        logging.info("🚀 Super Bridge: Tunnel + MITM + Dynamic LLM Hub")
        logging.info("=" * 50)

        # Start core components in daemon threads
        threading.Thread(target=start_local_proxy, daemon=True).start()
        threading.Thread(target=tunnel_worker, daemon=True).start()

        # Activate Native Bridge with separated Reader & Writer system
        start_native_bridge()

        # Main thread stays alive, waiting for daemon threads
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Exiting...")
        sys.exit(0)
    except Exception as e:
        logging.error(f"❌ Fatal error occurred: {e}")
        traceback.print_exc()
        # Avoid input() which can cause deadlocks with stdin
        time.sleep(5)
        sys.exit(1)


if __name__ == '__main__':
    main()
