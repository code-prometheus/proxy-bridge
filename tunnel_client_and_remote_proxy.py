# 主入口文件：组装子模块并启动所有服务，兼容原命令行执行方式
import sys
import os
import threading
import time
import logging

# 🚨 极重要防弹装甲：确保在任何打印或管道操作前，设置输入输出为强制二进制模式。这避免了 Native Messaging 在 Windows 下遇到 \n 换行符损坏的问题
if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

import utils
from local_proxy import start_local_proxy, nm_reader_thread
from remote_tunnel import tunnel_worker

if __name__ == '__main__':
    try:
        # 兼容原来的 --init-ca 命令，供 AutoSetup.bat 使用
        if len(sys.argv) > 1 and sys.argv[1] == '--init-ca':
            utils.CertManager.get_ca()
            sys.exit(0)

        logging.info("=" * 50)
        logging.info("🚀 Super Bridge: Tunnel + MITM + Dynamic LLM Hub (模块化版)")
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
        import traceback
        traceback.print_exc()
        input("按回车键退出...")
        sys.exit(1)