import json
import hashlib
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 全局预先接管并配置 Logging，彻底拔除 print()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ================= 核心配置 (从 settings.json 动态加载) =================
config_path = os.path.join(os.path.dirname(__file__), 'settings.json')

if not os.path.exists(config_path):
    logging.error(f"❌ 找不到配置文件: {config_path}")
    sys.exit(1)

try:
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    # 优先读取 server 独立节点配置，若不存在则智能兼容读取 client 节点的端口配置
    if 'server' in config:
        TUNNEL_IP = config['server'].get('tunnel_bind_ip', '0.0.0.0')
        TUNNEL_PORT = int(config['server'].get('tunnel_bind_port', config['client']['server_port']))
        PROXY_IP = config['server'].get('proxy_bind_ip', '127.0.0.1')
        PROXY_PORT = int(config['server'].get('proxy_bind_port', 60130))
    else:
        TUNNEL_IP = '0.0.0.0'
        TUNNEL_PORT = int(config['client']['server_port'])
        PROXY_IP = config['client'].get('local_proxy_ip', '127.0.0.1')
        PROXY_PORT = int(config['client'].get('local_proxy_port', 60130))

    SECRET_KEY = config['common']['secret_key'].encode('utf-8')
except Exception as e:
    logging.error(f"❌ 配置文件解析失败: {e}")
    sys.exit(1)
# ============================================

# 远端处理 SOCKS/HTTP 并发的线程池
proxy_executor = ThreadPoolExecutor(max_workers=200)


# --- RC4 流量伪装算法 ---
class RC4:
    def __init__(self, key: bytes):
        self.S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + self.S[i] + key[i % len(key)]) % 256
            self.S[i], self.S[j] = self.S[j], self.S[i]
        self.i = 0
        self.j = 0

    def process(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for k in range(len(data)):
            self.i = (self.i + 1) % 256
            self.j = (self.j + self.S[self.i]) % 256
            self.S[self.i], self.S[self.j] = self.S[self.j], self.S[self.i]
            out[k] = data[k] ^ self.S[(self.S[self.i] + self.S[self.j]) % 256]
        return bytes(out)


# --- 指令集定义 ---
CMD_PING = 1
CMD_PONG = 2
CMD_REQ_STREAM = 3
CMD_REP_STREAM = 4
CMD_DATA = 5
CMD_CLOSE_STREAM = 6
CMD_SYNC_CA = 7


# --- 全局隧道管理器 ---
class TunnelManager:
    def __init__(self):
        self.sock = None
        self.send_queue = queue.Queue()
        self.streams = {}
        self.stream_id_counter = 1
        self.lock = threading.Lock()
        self.rc4_rx = None
        self.rc4_tx = None
        self.connected = False
        self.last_recv_time = 0

    def setup_tunnel(self, conn):
        old_sock = self.sock
        self.sock = conn
        # 【核心修正】：引入底层硬件 Socket 超时约束。超过15秒没收到客户端数据，强制内核解除堵塞！
        self.sock.settimeout(15.0)

        rx_key = hashlib.sha256(SECRET_KEY + b'C2S').digest()
        tx_key = hashlib.sha256(SECRET_KEY + b'S2C').digest()

        self.rc4_rx = RC4(rx_key)
        self.rc4_tx = RC4(tx_key)
        self.connected = True
        self.last_recv_time = time.time()

        with self.send_queue.mutex:
            self.send_queue.queue.clear()

        with self.lock:
            for stream in self.streams.values():
                try:
                    stream['sock'].close()
                except:
                    pass
            self.streams.clear()

        if old_sock:
            try:
                old_sock.close()
            except:
                pass

    def generate_stream_id(self):
        with self.lock:
            sid = self.stream_id_counter
            self.stream_id_counter += 1
            return sid

    def send_packet(self, cmd, stream_id=0, payload=b''):
        if not self.connected: return
        header = struct.pack('!B I I', cmd, stream_id, len(payload))
        full_packet = header + payload
        frame = struct.pack('!I', len(full_packet)) + full_packet
        self.send_queue.put(frame)

    def close_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams:
                try:
                    self.streams[stream_id]['sock'].close()
                except:
                    pass
                del self.streams[stream_id]
        self.send_packet(CMD_CLOSE_STREAM, stream_id)


tunnel = TunnelManager()


def recvall_encrypted(sock, n, rc4_cipher):
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet: return None
        data.extend(packet)
    return rc4_cipher.process(bytes(data))


def tunnel_writer_thread():
    while True:
        current_sock = tunnel.sock
        try:
            if tunnel.connected and current_sock:
                frame = tunnel.send_queue.get(timeout=1.0)
                encrypted_frame = tunnel.rc4_tx.process(frame)
                current_sock.sendall(encrypted_frame)
            else:
                time.sleep(0.1)
        except queue.Empty:
            pass
        except Exception as e:
            if tunnel.sock == current_sock:
                logging.error(f"隧道发送异常: {e}")
                tunnel.connected = False


def tunnel_reader_thread():
    while True:
        current_sock = tunnel.sock
        if not tunnel.connected or not current_sock:
            time.sleep(0.1)
            continue

        try:
            rx = tunnel.rc4_rx
            len_bytes = recvall_encrypted(current_sock, 4, rx)
            if not len_bytes: raise Exception("隧道客户端主动断开或网络异常")

            tunnel.last_recv_time = time.time()

            frame_len = struct.unpack('!I', len_bytes)[0]
            packet_data = recvall_encrypted(current_sock, frame_len, rx)
            if not packet_data: raise Exception("隧道读数据包失败")

            cmd, stream_id, payload_len = struct.unpack('!B I I', packet_data[:9])
            payload = packet_data[9:]

            if cmd == CMD_PING:
                tunnel.send_packet(CMD_PONG)

            elif cmd == CMD_REP_STREAM:
                status = struct.unpack('!B', payload)[0]
                with tunnel.lock:
                    if stream_id in tunnel.streams:
                        tunnel.streams[stream_id]['status'] = (status == 0)
                        tunnel.streams[stream_id]['event'].set()

            elif cmd == CMD_DATA:
                with tunnel.lock:
                    if stream_id in tunnel.streams:
                        try:
                            tunnel.streams[stream_id]['sock'].sendall(payload)
                        except:
                            tunnel.close_stream(stream_id)

            elif cmd == CMD_CLOSE_STREAM:
                with tunnel.lock:
                    if stream_id in tunnel.streams:
                        try:
                            tunnel.streams[stream_id]['sock'].close()
                        except:
                            pass
                        del tunnel.streams[stream_id]

            # ================== 接收 Windows 发来的 CA 证书 ==================
            elif cmd == CMD_SYNC_CA:
                ca_path = os.path.expanduser('~/.proxy_bridge_ca.pem')
                try:
                    with open(ca_path, 'wb') as f:
                        f.write(payload)

                    curlrc_path = os.path.expanduser('~/.curlrc')
                    curlrc_content = ""
                    if os.path.exists(curlrc_path):
                        with open(curlrc_path, 'r') as f:
                            curlrc_content = f.read()

                    if ca_path not in curlrc_content:
                        with open(curlrc_path, 'a') as f:
                            f.write(f'\ncacert="{ca_path}"\n')

                    bashrc_path = os.path.expanduser('~/.bashrc')
                    bashrc_content = ""
                    if os.path.exists(bashrc_path):
                        with open(bashrc_path, 'r') as f:
                            bashrc_content = f.read()

                    if ca_path not in bashrc_content:
                        with open(bashrc_path, 'a') as f:
                            f.write(f'\n# Proxy Bridge Global CA Certs\n')
                            f.write(f'export REQUESTS_CA_BUNDLE="{ca_path}"\n')
                            f.write(f'export SSL_CERT_FILE="{ca_path}"\n')
                            f.write(f'export CURL_CA_BUNDLE="{ca_path}"\n')
                            f.write(f'export NODE_EXTRA_CA_CERTS="{ca_path}"\n')

                    logging.info("=" * 60)
                    logging.info(f"🎉 收到 Windows 客户端推送的 CA 根证书！(已存至 {ca_path})")
                    logging.info(f"✅ 已自动为您配置 ~/.curlrc (curl 自动信任)")
                    logging.info(f"✅ 已自动为您配置 ~/.bashrc (pip/requests/Node.js 自动信任)")
                    logging.info(f"💡 【重要】请在当前的 Ubuntu 终端执行一次以下命令使其立刻生效：")
                    logging.info(f"👉   source ~/.bashrc")
                    logging.info("=" * 60)
                except Exception as e:
                    logging.error(f"写入 CA 证书失败: {e}")

        except Exception as e:
            if tunnel.sock == current_sock:
                logging.error(f"❌ 隧道底层连接中断: {e}")
                tunnel.connected = False
                try:
                    current_sock.close()
                except:
                    pass
            time.sleep(1)


def pump_local_to_tunnel(local_sock, stream_id):
    try:
        while tunnel.connected:
            data = local_sock.recv(8192)
            if not data: break
            tunnel.send_packet(CMD_DATA, stream_id, data)
    except:
        pass
    finally:
        tunnel.close_stream(stream_id)


def handle_proxy_client(client_sock):
    if not tunnel.connected:
        logging.error("❌ 无活动隧道，拒绝本地请求")
        try:
            client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
        except:
            pass
        client_sock.close()
        return

    try:
        first_byte = client_sock.recv(1)
        if not first_byte:
            client_sock.close()
            return

        host = None
        port = None
        proto_id = 2
        init_data = b''
        response_to_local = b''

        if first_byte[0] == 0x05:
            proto_id = 2
            nmethods_byte = client_sock.recv(1)
            if nmethods_byte:
                nmethods = nmethods_byte[0]
                if nmethods > 0:
                    client_sock.recv(nmethods)

            client_sock.sendall(b'\x05\x00')
            req_data = client_sock.recv(256)
            if not req_data or req_data[0] != 0x05 or req_data[1] != 0x01:
                client_sock.close()
                return

            addr_type = req_data[3]
            if addr_type == 0x01:
                host = socket.inet_ntoa(req_data[4:8])
                port = struct.unpack('>H', req_data[8:10])[0]
            elif addr_type == 0x03:
                domain_len = req_data[4]
                host = req_data[5:5 + domain_len].decode('utf-8')
                port = struct.unpack('>H', req_data[5 + domain_len:7 + domain_len])[0]
            else:
                client_sock.close()
                return

            response_to_local = b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'

        else:
            header_data = first_byte
            while b'\r\n\r\n' not in header_data:
                chunk = client_sock.recv(4096)
                if not chunk: break
                header_data += chunk

            if b'\r\n\r\n' not in header_data:
                client_sock.close()
                return

            header_end = header_data.find(b'\r\n\r\n') + 4
            headers_only = header_data[:header_end]
            body_prefix = header_data[header_end:]

            first_line = headers_only.split(b'\r\n')[0].decode('utf-8', 'ignore')
            parts = first_line.split()
            if len(parts) < 3:
                client_sock.close()
                return
            method, url, _ = parts

            if method == 'CONNECT':
                proto_id = 1
                host_port = url.split(':')
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 443
                response_to_local = b'HTTP/1.1 200 Connection established\r\n\r\n'
                init_data = b''
            else:
                proto_id = 3
                
                # 【核心修复】：兼容处理直接请求（例如直接 curl 127.0.0.1/v1/messages）
                # 尝试从 Header 中提取 Host，作为兜底的转发地址
                target_host_header = ""
                for line in headers_only.split(b'\r\n')[1:]:
                    if line.lower().startswith(b'host:'):
                        target_host_header = line[5:].strip().decode('utf-8', 'ignore')
                        break

                if url.startswith('http://'): 
                    url_no_proto = url[7:]
                    path_pos = url_no_proto.find('/')
                    if path_pos == -1:
                        host_port = url_no_proto
                        path = '/'
                    else:
                        host_port = url_no_proto[:path_pos]
                        path = url_no_proto[path_pos:]
                else:
                    # 如果 url 不包含 http://（说明是直接路径如 /v1/messages），则依赖 Host 头
                    host_port = target_host_header
                    path = url

                if not host_port:
                    client_sock.close()
                    return

                host_port = host_port.split(':')
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 80

                lines = headers_only.split(b'\r\n')
                new_first_line = f"{method} {path} HTTP/1.1".encode('utf-8')
                lines[0] = new_first_line
                init_data = b'\r\n'.join(lines) + body_prefix
                response_to_local = b''

        if not host or not port:
            client_sock.close()
            return

        proto_map = {1: "HTTPS", 2: "SOCKS5", 3: "HTTP"}
        logging.info(f"⚡ 收到本地请求 -> [{proto_map[proto_id]}] {host}:{port}")

        stream_id = tunnel.generate_stream_id()
        connect_event = threading.Event()

        with tunnel.lock:
            tunnel.streams[stream_id] = {'sock': client_sock, 'event': connect_event, 'status': False}

        host_bytes = host.encode('utf-8')
        payload = struct.pack(f'!B H {len(host_bytes)}s H I', proto_id, len(host_bytes), host_bytes, port,
                              len(init_data)) + init_data
        tunnel.send_packet(CMD_REQ_STREAM, stream_id, payload)

        if connect_event.wait(timeout=10.0):
            with tunnel.lock:
                status = tunnel.streams.get(stream_id, {}).get('status', False)

            if status:
                if response_to_local:
                    client_sock.sendall(response_to_local)
                logging.info(f"✅ [Stream {stream_id}] 隧道链路打通，开始数据双向流动")
                pump_local_to_tunnel(client_sock, stream_id)
            else:
                try:
                    if proto_id == 2:
                        client_sock.sendall(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
                    elif proto_id in (1, 3):
                        client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                except Exception:
                    pass
                tunnel.close_stream(stream_id)
                logging.warning(f"❌ [Stream {stream_id}] Windows出网节点连接目标失败")
        else:
            try:
                if proto_id == 2:
                    client_sock.sendall(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00')
                elif proto_id in (1, 3):
                    client_sock.sendall(b'HTTP/1.1 504 Gateway Timeout\r\n\r\n')
            except Exception:
                pass
            tunnel.close_stream(stream_id)
            logging.warning(f"❌ [Stream {stream_id}] 隧道等待目标连接响应超时")

    except Exception as e:
        logging.error(f"代理处理异常: {e}")
        try:
            client_sock.close()
        except:
            pass


def tunnel_checker_thread():
    while True:
        time.sleep(3) # 高频巡检
        current_sock = tunnel.sock
        if tunnel.connected and current_sock and time.time() - tunnel.last_recv_time > 10:
            if tunnel.sock == current_sock:
                logging.warning("💔 隧道客户端心跳超时，判定为网络异常，强制清理僵尸连接释放通道...")
                tunnel.connected = False
                try:
                    # 强力切断底层，加速释放资源给下一任连接
                    current_sock.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                try:
                    current_sock.close()
                except:
                    pass


def listen_tunnel():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    server_sock.bind((TUNNEL_IP, TUNNEL_PORT))
    server_sock.listen(5)
    logging.info(f"✅ 隧道多路复用枢纽启动，等待Windows连接: {TUNNEL_IP}:{TUNNEL_PORT}")

    threading.Thread(target=tunnel_reader_thread, daemon=True).start()
    threading.Thread(target=tunnel_writer_thread, daemon=True).start()
    threading.Thread(target=tunnel_checker_thread, daemon=True).start()

    while True:
        try:
            conn, addr = server_sock.accept()
            logging.info(f"🚀 Windows隧道节点已连接: {addr}")
            tunnel.setup_tunnel(conn)
        except Exception as e:
            logging.error(f"隧道监听异常: {e}")


def listen_proxy():
    proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_sock.bind((PROXY_IP, PROXY_PORT))
    proxy_sock.listen(128)
    logging.info(f"✅ 本地SOCKS5/HTTP代理端口就绪: {PROXY_IP}:{PROXY_PORT}")

    while True:
        try:
            client_sock, addr = proxy_sock.accept()
            # 引入 Executor 并发调度远程请求，不阻塞主监听
            proxy_executor.submit(handle_proxy_client, client_sock)
        except Exception as e:
            logging.error(f"代理监听异常: {e}")


if __name__ == '__main__':
    threading.Thread(target=listen_tunnel, daemon=True).start()
    listen_proxy()