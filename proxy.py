"""
Proxy Bridge v3.0 — Proxy core.
MITM TLS termination + HTTP forwarding via Chrome Native Messaging.

Architecture (layered):
    client → proxy.py (accept + MITM) → http.py (parse)
           → upstream.py (forward) → nm.py (Chrome fetch)
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
import nm

logger = logging.getLogger('proxy_bridge.proxy')

# Concurrency control
MAX_WORKERS = 200
MAX_INFLIGHT = 200
proxy_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
inflight_semaphore = Semaphore(MAX_INFLIGHT)

# Platform-specific socket options
if sys.platform == 'win32':
    _SOCKET_EXCL_OPT = (socket.SO_EXCLUSIVEADDRUSE, 1)
    _SOCKET_REUSE_OPT = None
else:
    _SOCKET_EXCL_OPT = None
    _SOCKET_REUSE_OPT = (socket.SO_REUSEADDR, 1)


def _reject_connection(client_sock: socket.socket) -> None:
    """Reject an over-capacity connection gracefully."""
    try:
        client_sock.sendall(
            b'HTTP/1.1 503 Service Unavailable\r\n'
            b'Content-Length: 0\r\nConnection: close\r\n\r\n')
    except OSError:
        pass
    try:
        client_sock.close()
    except OSError:
        pass


# ===========================================================================
# Client handler — entry point for each connection
# ===========================================================================

def handle_client(raw_sock: socket.socket) -> None:
    """Handle one client connection: parse CONNECT or HTTP, forward, respond."""
    conn = Connection(raw_sock)
    try:
        # Read the first request
        req = read_request(conn)
        if req is None:
            return

        host, port = _extract_host_port(req)
        if host is None:
            write_response(conn, HttpResponse(
                400, 'Bad Request', {},
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


# ===========================================================================
# CONNECT (MITM) handling
# ===========================================================================

def _handle_connect(conn: Connection, host: str, port: int) -> None:
    """CONNECT method: send 200, wrap TLS, run MITM loop."""
    try:
        conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    except OSError:
        return

    _run_mitm(conn, host, port)


def _run_mitm(conn: Connection, host: str, port: int) -> None:
    """Wrap client socket as TLS server with per-host cert, run request loop."""
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


def _mitm_loop(tls_conn: Connection, host: str, port: int) -> None:
    """Read decrypted HTTP requests from TLS socket, forward, write responses.

    Supports HTTP keep-alive up to 100 requests per connection.
    """
    max_requests = 100
    for _ in range(max_requests):
        req = read_request(tls_conn)
        if req is None:
            break

        # Build absolute URL — everything is https over CONNECT
        scheme = 'https' if port == 443 else 'http'
        if req.url.startswith('http://') or req.url.startswith('https://'):
            full_url = req.url
        elif (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
            full_url = f"{scheme}://{host}{req.url}"
        else:
            full_url = f"{scheme}://{host}:{port}{req.url}"

        logger.debug("MITM request: %s %s (body=%d)", req.method, full_url, len(req.body))

        # Update request with absolute URL for forwarding
        req = HttpRequest(method=req.method, url=full_url,
                          headers=req.headers, body=req.body)

        resp = forward(req, tls_conn)
        # forward() streams headers+body via conn for NM; write_response
        # only needed for error/urllib responses with populated body
        if resp.body:
            write_response(tls_conn, resp)

        # Honour client's Connection: close
        if req.get_header('Connection').lower() == 'close':
            break


# ===========================================================================
# Plain HTTP handling
# ===========================================================================

def _handle_http(conn: Connection, req: HttpRequest, host: str, port: int) -> None:
    """Handle a non-CONNECT HTTP request."""
    # Build absolute URL
    scheme = 'https' if port == 443 else 'http'
    if req.url.startswith('http://') or req.url.startswith('https://'):
        full_url = req.url
    elif (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
        full_url = f"{scheme}://{host}{req.url}"
    else:
        full_url = f"{scheme}://{host}:{port}{req.url}"

    logger.debug("HTTP request: %s %s (body=%d)", req.method, full_url, len(req.body))

    req = HttpRequest(method=req.method, url=full_url,
                      headers=req.headers, body=req.body)
    resp = forward(req, conn)
    write_response(conn, resp)


# ===========================================================================
# Host/port extraction
# ===========================================================================

def _extract_host_port(req: HttpRequest):
    """Extract (host, port) from request Host header or URL.

    Returns (None, None) if host cannot be determined.
    """
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
        if req.method == 'CONNECT':
            port = 443
        else:
            port = 80

    return host, port


# ===========================================================================
# Proxy server — accept loop
# ===========================================================================

def start_proxy_server(shutdown_evt: threading.Event = None) -> None:
    """Bind socket and accept client connections in a loop.

    Runs on main thread. Accepts connections and submits to ThreadPoolExecutor.
    Uses inflight_semaphore to limit concurrent connections.

    Exits when shutdown_evt is set (NM disconnected — Chrome restarting).
    Uses SO_REUSEADDR on Linux to allow quick rebind by new process.
    """
    bind_addr = (utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    if _SOCKET_EXCL_OPT:
        server_sock.setsockopt(socket.SOL_SOCKET, *_SOCKET_EXCL_OPT)
    if _SOCKET_REUSE_OPT:
        server_sock.setsockopt(socket.SOL_SOCKET, *_SOCKET_REUSE_OPT)

    # Bind with retry (60s window for Chrome to reconnect)
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
    server_sock.settimeout(1.0)  # Non-blocking accept to check shutdown_evt

    logger.info("Proxy server listening on %s:%d",
                utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)

    while True:
        # Check if NM disconnected → exit so new process can bind
        if shutdown_evt is not None and shutdown_evt.is_set():
            logger.info("NM shutdown signal received — closing accept loop")
            break

        try:
            client_sock, client_addr = server_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            continue

        logger.debug("Accepted connection from %s:%d",
                     client_addr[0], client_addr[1])

        # Rate-limit via semaphore
        if inflight_semaphore.acquire(blocking=False):
            def _guarded_handle(sock):
                try:
                    handle_client(sock)
                finally:
                    inflight_semaphore.release()

            proxy_executor.submit(_guarded_handle, client_sock)
        else:
            _reject_connection(client_sock)

    # Graceful shutdown: stop accept, drain inflight briefly, then exit
    logger.info("Shutting down proxy server...")
    server_sock.close()
    proxy_executor.shutdown(wait=False)
    time.sleep(0.5)  # brief grace for inflight connections
