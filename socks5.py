"""
Proxy Bridge — SOCKS5 standalone module (pure TCP tunneling, no Chrome).
Called via os.fork() from proxy.py for every SOCKS5 connection.

Handshake → CONNECT → socket.connect to target → bidirectional raw byte relay.
Any TCP protocol works (HTTP, TLS, SSH, RDP...). Chrome is NOT involved.
"""
import logging
import os
import select
import socket

logger = logging.getLogger('proxy_bridge.socks5')


def _recv_exact(conn, n):
    data = b''
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _socks5_handshake(conn):
    """Server greeting: VER=5, NMETHODS, METHODS → reply no-auth (0x00)."""
    data = _recv_exact(conn, 2)
    if data is None or data[0] != 0x05:
        return False
    nmethods = data[1]
    if nmethods > 0 and _recv_exact(conn, nmethods) is None:
        return False
    try:
        conn.sendall(b'\x05\x00')
    except OSError:
        return False
    return True


def _read_connect(conn):
    """Read CONNECT request. Returns (host, port) or (None, None)."""
    head = _recv_exact(conn, 4)
    if head is None or head[0] != 0x05 or head[1] != 0x01:
        try:
            conn.sendall(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')  # cmd not supported
        except OSError:
            pass
        return None, None
    atyp = head[3]
    host = None
    if atyp == 0x01:  # IPv4
        addr = _recv_exact(conn, 4)
        if addr is None:
            return None, None
        host = socket.inet_ntoa(addr)
    elif atyp == 0x03:  # domain
        ln = _recv_exact(conn, 1)
        if ln is None:
            return None, None
        domain = _recv_exact(conn, ln[0])
        if domain is None:
            return None, None
        host = domain.decode('utf-8', errors='replace')
    elif atyp == 0x04:  # IPv6
        addr = _recv_exact(conn, 16)
        if addr is None:
            return None, None
        host = socket.inet_ntop(socket.AF_INET6, addr)
    else:
        try:
            conn.sendall(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')  # addr type not supported
        except OSError:
            pass
        return None, None
    port_data = _recv_exact(conn, 2)
    if port_data is None:
        return None, None
    return host, int.from_bytes(port_data, 'big')


def handle_connection(client_sock):
    """Full SOCKS5 session on an accepted connection (already Peeked as 0x05)."""
    try:
        client_sock.settimeout(30)

        if not _socks5_handshake(client_sock):
            return
        host, port = _read_connect(client_sock)
        if host is None:
            return

        try:
            target = socket.create_connection((host, port), timeout=30)
        except OSError as e:
            logger.debug("SOCKS5 connect %s:%d failed: %s", host, port, e)
            try:
                # 0x05 0x05 = connection refused
                client_sock.sendall(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
            except OSError:
                pass
            return

        client_sock.settimeout(None)
        target.settimeout(None)
        # Success reply: VER=5, REP=0, RSV=0, ATYP=1, BND.ADDR=0.0.0.0, BND.PORT=0
        client_sock.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        logger.debug("SOCKS5 tunnel established %s:%d (pid=%d)",
                     host, port, os.getpid())

        # Bidirectional raw byte relay
        while True:
            r, _, _ = select.select([client_sock, target], [], [])
            if client_sock in r:
                data = client_sock.recv(65536)
                if not data:
                    break
                target.sendall(data)
            if target in r:
                data = target.recv(65536)
                if not data:
                    break
                client_sock.sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            client_sock.close()
        except OSError:
            pass
