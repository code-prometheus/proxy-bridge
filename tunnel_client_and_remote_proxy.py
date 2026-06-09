import sys
import os
import threading
import time
import logging
import traceback
import utils

from local_proxy import start_local_proxy, nm_reader_thread
from remote_tunnel import tunnel_worker

if __name__ == '__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
            utils.CertManager.get_ca()
            sys.exit(0)

        logging.info("=" * 50)
        logging.info("🚀 Super Bridge: Tunnel + MITM + Dynamic LLM Hub")
        logging.info("=" * 50)

        # 🛡️ 将所有核心组件移入守护线程，保护主线程不死
        threading.Thread(target=start_local_proxy, daemon=True).start()
        threading.Thread(target=tunnel_worker, daemon=True).start()
        threading.Thread(target=nm_reader_thread, daemon=True).start()

        # 主线程永远存活，挂起等待
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Exiting...")
        sys.exit(0)
    except Exception as e:
        logging.error(f"❌ 程序发生致命崩溃: {e}")
        traceback.print_exc()
        input("按回车键退出...")
        sys.exit(1)