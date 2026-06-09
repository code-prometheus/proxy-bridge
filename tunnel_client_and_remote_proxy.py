import logging
import sys
import threading
import time
import traceback

import utils
# 导入全新的基于分离 I/O 模型启动方法
from local_proxy import start_local_proxy, start_native_bridge
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

        # 激活 Native Bridge 的完全分离 Reader & Writer 系统
        start_native_bridge()

        # 主线程永远存活，挂起等待
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Exiting...")
        sys.exit(0)
    except Exception as e:
        logging.error(f"❌ 程序发生致命崩溃: {e}")
        traceback.print_exc()
        # 移除 input()，杜绝它争夺系统终端 stdin 读取导致 NM 发生死锁崩溃
        time.sleep(5)
        sys.exit(1)
