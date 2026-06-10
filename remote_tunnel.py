import socket
import struct
import threading
import time
import queue
import hashlib
import logging
import utils

class ClientMultiplexer:
    def __init__(self):
        self.sock = None
        self.send_queue = queue.Queue()
        self.streams = {}
        self.lock = threading.Lock()
        self.connected = False

    def send_packet(self, cmd, stream_id=0, payload=b''):
        if not self.connected: return
        header = struct.pack('!B I I', cmd, stream_id, len(payload))
        frame = struct.pack('!I', len(header) + len(payload)) + header + payload
        self.send_queue.put(frame)

    def close_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams:
                try: self.streams[stream_id].close()
                except: pass
                del self.streams[stream_id]
        self.send_packet(6, stream_id)

client_mux = ClientMultiplexer()

def pump_data(src, stream_id):
    try:
        while client_mux.connected:
            data = src.recv(8192)
            if not data: break
            client_mux.send_packet(5, stream_id, data)
    except: pass
    finally: client_mux.close_stream(stream_id)

def handle_new_tunnel_stream(stream_id, payload):
    try:
        proto_id = struct.unpack('!B', payload[:1])[0]
        offset = 1
        host_len = struct.unpack('!H', payload[offset:offset+2])[0]; offset += 2
        host = struct.unpack(f'!{host_len}s', payload[offset:offset+host_len])[0].decode('utf-8'); offset += host_len
        port = struct.unpack('!H', payload[offset:offset+2])[0]; offset += 2
        init_data_len = struct.unpack('!I', payload[offset:offset+4])[0]; offset += 4
        init_data = payload[offset:offset+init_data_len]

        target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_sock.settimeout(5.0)

        if utils.CHROME_CONNECTED and proto_id in (1, 3):
            target_sock.connect((utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT))
            if proto_id == 1: 
                target_sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode('utf-8'))
                resp = b''
                while b'\r\n\r\n' not in resp:
                    chunk = target_sock.recv(1024)
                    if not chunk: break
                    resp += chunk
            else:
                target_sock.sendall(init_data)
        else:
            target_sock.connect((host, port))
            if init_data: target_sock.sendall(init_data)

        target_sock.settimeout(None)
        with client_mux.lock:
            client_mux.streams[stream_id] = target_sock

        client_mux.send_packet(4, stream_id, struct.pack('!B', 0))
        threading.Thread(target=pump_data, args=(target_sock, stream_id), daemon=True).start()
    except Exception as e:
        client_mux.send_packet(4, stream_id, struct.pack('!B', 1))

def tunnel_worker():
    while True:
        try:
            logging.info(f"🔄 [RC4 Tunnel] 尝试连接到服务器 {utils.SERVER_ADDR}:{utils.SERVER_PORT} ...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(10.0)
            sock.connect((utils.SERVER_ADDR, utils.SERVER_PORT))
            
            # 【核心修正】：设置 Socket 物理硬件超时，防止假死挂起！保证断网后 recv 瞬间解开
            sock.settimeout(15.0) 
            
            rx_key = hashlib.sha256(utils.SECRET_KEY + b'S2C').digest()
            tx_key = hashlib.sha256(utils.SECRET_KEY + b'C2S').digest()
            rc4_rx = utils.RC4(rx_key)
            rc4_tx = utils.RC4(tx_key)

            client_mux.sock = sock
            client_mux.connected = True
            logging.info("🔗 [RC4 Tunnel] 底层加密隧道已成功连接远端服务器！")

            try:
                with open(utils.CertManager.CA_CERT_PATH, 'rb') as f: ca_data = f.read()
                client_mux.send_packet(7, 0, ca_data)
            except Exception: pass

            # 【核心修正】：用字典包装心跳时间戳，规避多线程闭包陷阱脏读问题
            state = {'last_recv_time': time.time()}
            
            def heartbeat_daemon():
                while client_mux.connected:
                    time.sleep(3)
                    if not client_mux.connected: break
                    try: client_mux.send_packet(1)
                    except: pass
                    
                    if time.time() - state['last_recv_time'] > 10:
                        logging.warning("💔 [RC4 Tunnel] 隧道心跳超时(>10秒)，强制切断僵尸连接以触发重连...")
                        client_mux.connected = False
                        try: 
                            client_mux.sock.shutdown(socket.SHUT_RDWR) 
                        except: pass
                        try: client_mux.sock.close() 
                        except: pass
                        break
            threading.Thread(target=heartbeat_daemon, daemon=True).start()

            def writer():
                while client_mux.connected:
                    try:
                        frame = client_mux.send_queue.get(timeout=1.0)
                        client_mux.sock.sendall(rc4_tx.process(frame))
                    except queue.Empty: pass
                    except: client_mux.connected = False
            threading.Thread(target=writer, daemon=True).start()

            def recv_enc(n):
                d = bytearray()
                while len(d) < n:
                    p = sock.recv(n - len(d))
                    if not p: return None
                    d.extend(p)
                return rc4_rx.process(bytes(d))

            while client_mux.connected:
                len_b = recv_enc(4)
                if not len_b: raise Exception("对端主动断开或网络异常")
                packet = recv_enc(struct.unpack('!I', len_b)[0])
                if not packet: raise Exception("读取包错误")
                
                state['last_recv_time'] = time.time()
                cmd, stream_id, _ = struct.unpack('!B I I', packet[:9])
                payload = packet[9:]

                if cmd == 2: continue 
                elif cmd == 3: threading.Thread(target=handle_new_tunnel_stream, args=(stream_id, payload), daemon=True).start()
                elif cmd == 5:
                    with client_mux.lock:
                        if stream_id in client_mux.streams:
                            try: client_mux.streams[stream_id].sendall(payload)
                            except: client_mux.close_stream(stream_id)
                elif cmd == 6: client_mux.close_stream(stream_id)

        except Exception as e:
            # 【核心修正】：增加显式的异常捕获日志输出！之前就是因为默默吞掉了异常，导致你看不见它正在重连
            logging.warning(f"⚠️ [RC4 Tunnel] 隧道连接断裂或发生网络异常: {e}")
        finally:
            client_mux.connected = False
            try:
                if client_mux.sock: client_mux.sock.close()
            except: pass
            
            logging.info("⏳ [RC4 Tunnel] 准备在 3 秒后尝试重新建立隧道连接...")
            time.sleep(3)