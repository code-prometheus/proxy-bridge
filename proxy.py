"""
Proxy Bridge v3.0 — Proxy core.
MITM TLS termination + HTTP forwarding via Chrome Native Messaging.
"""
import logging
import os
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore

import utils
from certs import CertManager
from connection import Connection
from http_parser import read_request, write_response, HttpRequest, HttpResponse
from upstream import forward

logger = logging.getLogger('proxy_bridge.proxy')

MAX_WORKERS = 200
MAX_INFLIGHT = 200
proxy_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
inflight_semaphore = Semaphore(MAX_INFLIGHT)

if sys.platform == 'win32':
    _SOCKET_EXCL_OPT = (socket.SO_EXCLUSIVEADDRUSE, 1)
    _SOCKET_REUSE_OPT = None
else:
    _SOCKET_EXCL_OPT = None
    _SOCKET_REUSE_OPT = (socket.SO_REUSEADDR, 1)


def _reject_connection(client_sock):
    try:
        client_sock.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                            b'Content-Length: 0\r\nConnection: close\r\n\r\n')
    except OSError:
        pass
    try:
        client_sock.close()
    except OSError:
        pass


def handle_client(raw_sock):
    conn = Connection(raw_sock)
    try:
        req = read_request(conn)
        if req is None:
            return
        host, port = _extract_host_port(req)
        if host is None:
            write_response(conn, HttpResponse(400, 'Bad Request', {},
                                              b'Missing Host header'))
            return
        logger.debug("handle_client %s %s -> %s:%d", req.method, req.url, host, port)
        if req.method == 'CONNECT':
            _handle_connect(conn, host, port)
        else:
            _handle_http(conn, req, host, port)
    except Exception as e:
        logger.debug("handle_client error: %s", e)
    finally:
        conn.close()


def _handle_connect(conn, host, port):
    try:
        conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    except OSError:
        return
    _run_mitm(conn, host, port)


def _run_mitm(conn, host, port):
    host_clean = host.split(':')[0]
    cert_path, key_path = CertManager.get_cert_for_host(host_clean)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(cert_path, key_path)
    try:
        tls_conn = Connection(conn._raw, tls_ctx=ssl_context)
    except Exception as e:
        logger.debug("MITM TLS handshake failed for %s: %s", host, e)
        return
    try:
        _mitm_loop(tls_conn, host, port)
    except Exception as e:
        logger.debug("MITM loop error for %s: %s", host, e)
    finally:
        tls_conn.close()


def _mitm_loop(tls_conn, host, port):
    for _ in range(100):
        req = read_request(tls_conn)
        if req is None:
            break
        scheme = 'https' if port == 443 else 'http'
        if req.url.startswith('http://') or req.url.startswith('https://'):
            full_url = req.url
        elif (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
            full_url = f"{scheme}://{host}{req.url}"
        else:
            full_url = f"{scheme}://{host}:{port}{req.url}"
        logger.debug("MITM request: %s %s (body=%d)", req.method, full_url, len(req.body))
        forward(HttpRequest(method=req.method, url=full_url,
                            headers=req.headers, body=req.body), tls_conn)
        if req.get_header('Connection').lower() == 'close':
            break


def _handle_http(conn, req, host, port):
    scheme = 'https' if port == 443 else 'http'
    if req.url.startswith('http://') or req.url.startswith('https://'):
        full_url = req.url
    elif (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
        full_url = f"{scheme}://{host}{req.url}"
    else:
        full_url = f"{scheme}://{host}:{port}{req.url}"
    logger.debug("HTTP request: %s %s (body=%d)", req.method, full_url, len(req.body))
    forward(HttpRequest(method=req.method, url=full_url,
                        headers=req.headers, body=req.body), conn)


def _extract_host_port(req):
    host_header = req.get_header('Host')
    if not host_header:
        return None, None
    if ':' in host_header:
        host, port_str = host_header.rsplit(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 80
    else:
        host = host_header
        port = 443 if req.method == 'CONNECT' else 80
    return host, port


def start_proxy_server(shutdown_evt=None):
    bind_addr = (utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if _SOCKET_EXCL_OPT:
        server_sock.setsockopt(socket.SOL_SOCKET, *_SOCKET_EXCL_OPT)
    if _SOCKET_REUSE_OPT:
        server_sock.setsockopt(socket.SOL_SOCKET, *_SOCKET_REUSE_OPT)
    for attempt in range(30):
        try:
            server_sock.bind(bind_addr)
            break
        except OSError:
            if attempt == 29:
                logger.error("Failed to bind %s:%d after 30 attempts — exiting",
                             utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
                os._exit(0)
            time.sleep(2)
    server_sock.listen(512)
    server_sock.settimeout(1.0)
    logger.info("Proxy server listening on %s:%d", utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
    while True:
        if shutdown_evt is not None and shutdown_evt.is_set():
            logger.info("NM shutdown signal received — closing accept loop")
            break
        try:
            client_sock, client_addr = server_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            continue
        logger.debug("Accepted connection from %s:%d", client_addr[0], client_addr[1])
        if inflight_semaphore.acquire(blocking=False):
            def _guarded_handle(sock):
                try:
                    handle_client(sock)
                finally:
                    inflight_semaphore.release()
            proxy_executor.submit(_guarded_handle, client_sock)
        else:
            _reject_connection(client_sock)
    server_sock.close()
    proxy_executor.shutdown(wait=False)
    time.sleep(0.5)
