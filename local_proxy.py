import base64
import json
import logging
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import utils

logger = logging.getLogger(__name__)

proxy_executor = ThreadPoolExecutor(max_workers=500)


def _read_http_header(sock):
    """Read until \\r\\n\\r\\n. Return (method, url, headers_dict, body_prefix_bytes) or (None,None,None,None)."""
    data = b""
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(4096)
        except Exception as e:
            logger.debug("_read_http_header recv error: %s", e)
            return None, None, None, None
        if not chunk:
            return None, None, None, None
        data += chunk
        if len(data) > 65536:
            logger.warning("HTTP header too large, truncating")
            return None, None, None, None

    header_end = data.find(b"\r\n\r\n")
    header_bytes = data[:header_end]
    body_prefix = data[header_end + 4:]

    header_text = header_bytes.decode("utf-8", errors="replace")
    lines = header_text.split("\r\n")

    if not lines:
        return None, None, None, None

    request_line = lines[0]
    parts = request_line.split(" ", 2)
    if len(parts) < 2:
        return None, None, None, None

    method = parts[0].upper()
    url = parts[1]
    http_version = parts[2] if len(parts) > 2 else "HTTP/1.1"

    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            headers[key] = value

    return method, url, headers, body_prefix


def _read_chunked_body(sock, body_prefix):
    """Parse chunked transfer encoding, return full body bytes."""
    body = b""
    data = body_prefix
    while True:
        while b"\r\n" not in data:
            try:
                chunk = sock.recv(4096)
            except Exception as e:
                logger.debug("_read_chunked_body recv error: %s", e)
                return body
            if not chunk:
                return body
            data += chunk

        size_end = data.find(b"\r\n")
        size_line = data[:size_end].decode("utf-8", errors="replace").strip()
        data = data[size_end + 2:]

        size_line = size_line.split(";")[0].strip()
        try:
            chunk_size = int(size_line, 16)
        except ValueError:
            logger.debug("Invalid chunk size: %r", size_line)
            return body

        if chunk_size == 0:
            while b"\r\n\r\n" not in data:
                try:
                    chunk = sock.recv(4096)
                except Exception:
                    return body
                if not chunk:
                    return body
                data += chunk
            return body

        while len(data) < chunk_size + 2:
            try:
                chunk = sock.recv(max(4096, chunk_size + 2 - len(data)))
            except Exception as e:
                logger.debug("_read_chunked_body chunk data recv error: %s", e)
                return body
            if not chunk:
                return body
            data += chunk

        body += data[:chunk_size]
        data = data[chunk_size + 2:]


def _read_content_length_body(sock, body_prefix, content_length):
    """Read exact content_length bytes, return body."""
    body = body_prefix
    remaining = content_length - len(body_prefix)
    while remaining > 0:
        try:
            chunk = sock.recv(min(65536, remaining))
        except Exception as e:
            logger.debug("_read_content_length_body recv error: %s", e)
            return body
        if not chunk:
            break
        body += chunk
        remaining -= len(chunk)
    return body[:content_length]


def _build_response_head(status, status_text, headers_dict, body_len, is_chunked=False):
    """Build HTTP response header bytes. Handle set-cookie array from Chrome NM."""
    drop = {"connection", "proxy-connection", "keep-alive", "content-length", "transfer-encoding", "content-encoding"}
    head = "HTTP/1.1 %d %s\r\n" % (status, status_text)
    for k, v in headers_dict.items():
        kl = k.lower()
        if kl == "set-cookie" and isinstance(v, list):
            for cv in v:
                head += "Set-Cookie: %s\r\n" % cv
        elif kl not in drop:
            head += "%s: %s\r\n" % (k, v)
    if is_chunked:
        head += "Transfer-Encoding: chunked\r\n"
    else:
        head += "Content-Length: %d\r\n" % body_len
    head += "Connection: close\r\n\r\n"
    return head.encode("utf-8")


def _forward_via_nm(sock, method, url, headers, body):
    """Forward request through Chrome Native Messaging and stream response back."""
    clean_headers = {}
    drop_request = {"connection", "proxy-connection", "keep-alive", "host"}
    for k, v in headers.items():
        kl = k.lower()
        if kl not in drop_request:
            clean_headers[k] = v

    chunk_size = 65536

    start_msg = {
        "type": "request_start",
        "method": method,
        "url": url,
        "headers": clean_headers,
    }
    utils.nm_send_msg(start_msg)

    if body:
        offset = 0
        while offset < len(body):
            chunk = body[offset:offset + chunk_size]
            offset += chunk_size
            chunk_b64 = base64.b64encode(chunk).decode("ascii")
            utils.nm_send_msg({"type": "request_body", "data": chunk_b64})
            if len(chunk) < chunk_size:
                break

    utils.nm_send_msg({"type": "request_end"})

    response_event = threading.Event()
    response_data = {"status": 502, "status_text": "Bad Gateway", "headers": {}, "body": b"", "complete": False}

    def on_response(msg):
        msg_type = msg.get("type", "")
        if msg_type == "response_start":
            response_data["status"] = msg.get("status", 200)
            response_data["status_text"] = msg.get("statusText", "OK")
            resp_headers = msg.get("headers", {})
            if "set-cookie" in resp_headers and isinstance(resp_headers["set-cookie"], str):
                resp_headers["set-cookie"] = resp_headers["set-cookie"].split("\n")
            response_data["headers"] = resp_headers
            response_event.set()
        elif msg_type == "response_body":
            b64_data = msg.get("data", "")
            if b64_data:
                response_data["body"] += base64.b64decode(b64_data)
        elif msg_type == "response_end":
            response_data["complete"] = True
            response_event.set()

    req_key = str(time.time())
    utils.nm_pending_requests[req_key] = on_response
    try:
        while not response_event.wait(15):
            pass
        response_event.clear()
        while not response_data["complete"]:
            if response_event.wait(10):
                response_event.clear()
    finally:
        utils.nm_pending_requests.pop(req_key, None)

    resp_headers = response_data["headers"]
    resp_body = response_data["body"]
    status = response_data["status"]
    status_text = response_data["status_text"]

    head = _build_response_head(status, status_text, resp_headers, len(resp_body), is_chunked=False)

    try:
        sock.sendall(head + resp_body)
    except Exception as e:
        logger.debug("_forward_via_nm send response error: %s", e)


def _forward_via_urllib(sock, method, url, headers, body):
    """Fallback: use urllib for direct HTTP request."""
    clean_headers = {}
    drop_request = {"connection", "proxy-connection", "keep-alive", "host"}
    for k, v in headers.items():
        kl = k.lower()
        if kl not in drop_request:
            clean_headers[k] = v

    data = body if body else None
    req = urllib.request.Request(url, data=data, headers=clean_headers, method=method)

    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception as e:
        logger.debug("_forward_via_urllib request error: %s", e)
        error_body = b"Proxy error: " + str(e).encode("utf-8")
        head = _build_response_head(502, "Bad Gateway", {}, len(error_body))
        try:
            sock.sendall(head + error_body)
        except Exception:
            pass
        return

    status = resp.status
    status_text = resp.reason if hasattr(resp, "reason") else "OK"
    raw_headers = dict(resp.headers)
    body_bytes = resp.read()

    head = _build_response_head(status, status_text, raw_headers, len(body_bytes), is_chunked=False)

    try:
        sock.sendall(head + body_bytes)
    except Exception as e:
        logger.debug("_forward_via_urllib send response error: %s", e)


def handle_http_request(sock, method, url, headers, body_prefix, host, port):
    """Main HTTP handler: read body, determine URL, forward via NM or urllib."""
    body = body_prefix
    transfer_encoding = headers.get("Transfer-Encoding", "").lower()
    content_length_raw = headers.get("Content-Length")

    if transfer_encoding == "chunked":
        body = _read_chunked_body(sock, body_prefix)
    elif content_length_raw is not None:
        try:
            content_length = int(content_length_raw)
        except ValueError:
            content_length = 0
        body = _read_content_length_body(sock, body_prefix, content_length)
    else:
        body = body_prefix if body_prefix else b""

    if url.startswith("http://") or url.startswith("https://"):
        full_url = url
    else:
        scheme = "https" if port == 443 else "http"
        if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
            full_url = "%s://%s%s" % (scheme, host, url)
        else:
            full_url = "%s://%s:%d%s" % (scheme, host, port, url)

    logger.debug("handle_http_request %s %s (host=%s port=%s)", method, full_url, host, port)

    if utils.CHROME_CONNECTED:
        _forward_via_nm(sock, method, full_url, headers, body)
    else:
        _forward_via_urllib(sock, method, full_url, headers, body)


def handle_connect_tunnel(client_sock, host, port):
    """MITM HTTPS: send 200, wrap TLS, parse inner request, forward."""
    try:
        client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    except Exception as e:
        logger.debug("handle_connect_tunnel send 200 error: %s", e)
        return

    cert_path, key_path = utils.CertManager.get_ca()

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        tls_sock = context.wrap_socket(client_sock, server_side=True)
    except Exception as e:
        logger.debug("handle_connect_tunnel TLS wrap error: %s", e)
        return

    method, url, headers, body_prefix = _read_http_header(tls_sock)
    if method is None:
        try:
            tls_sock.close()
        except Exception:
            pass
        return

    inner_host = host
    inner_port = 443

    handle_http_request(tls_sock, method, url, headers, body_prefix, inner_host, inner_port)

    try:
        tls_sock.close()
    except Exception:
        pass


def handle_client(client_sock):
    """Entry point for each connection."""
    try:
        client_sock.settimeout(30)

        method, url, headers, body_prefix = _read_http_header(client_sock)
        if method is None:
            client_sock.close()
            return

        host_header = headers.get("Host", "")
        if not host_header:
            error_body = b"Missing Host header"
            err = _build_response_head(400, "Bad Request", {}, len(error_body))
            try:
                client_sock.sendall(err + error_body)
            except Exception:
                pass
            client_sock.close()
            return

        if ":" in host_header:
            host, port_str = host_header.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 80
        else:
            host = host_header
            if method == "CONNECT":
                port = 443
            else:
                port = 80

        logger.debug("handle_client %s %s -> %s:%d", method, url, host, port)

        if method == "CONNECT":
            handle_connect_tunnel(client_sock, host, port)
        else:
            handle_http_request(client_sock, method, url, headers, body_prefix, host, port)

    except Exception as e:
        logger.debug("handle_client error: %s", e)
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


def start_proxy_server():
    """Bind socket and accept loop."""
    bind_addr = (utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(bind_addr)
    server_sock.listen(512)

    logger.info("Proxy server listening on %s:%d", utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)

    while True:
        try:
            client_sock, client_addr = server_sock.accept()
            logger.debug("Accepted connection from %s:%d", client_addr[0], client_addr[1])
            proxy_executor.submit(handle_client, client_sock)
        except Exception as e:
            logger.debug("accept error: %s", e)
            time.sleep(0.1)


def native_writer_thread():
    """Drain utils.nm_send_queue, write length-prefixed JSON to stdout."""
    while True:
        try:
            msg = utils.nm_send_queue.get()
            if msg is None:
                break
            json_data = json.dumps(msg, ensure_ascii=False)
            json_bytes = json_data.encode("utf-8")
            length_bytes = struct.pack("<I", len(json_bytes))
            sys.stdout.buffer.write(length_bytes + json_bytes)
            sys.stdout.buffer.flush()
        except Exception as e:
            logger.debug("native_writer_thread error: %s", e)
            break


def native_reader_thread():
    """Read length-prefixed JSON from stdin, route to utils.nm_pending_requests."""
    while True:
        try:
            raw_length = sys.stdin.buffer.read(4)
            if not raw_length or len(raw_length) < 4:
                break
            msg_length = struct.unpack("<I", raw_length)[0]
            if msg_length > 16 * 1024 * 1024:
                logger.warning("NM message too large: %d bytes, skipping", msg_length)
                continue
            json_bytes = sys.stdin.buffer.read(msg_length)
            if not json_bytes or len(json_bytes) < msg_length:
                break
            msg = json.loads(json_bytes.decode("utf-8", errors="replace"))
            for handler in list(utils.nm_pending_requests.values()):
                try:
                    handler(msg)
                except Exception as e:
                    logger.debug("NM handler error: %s", e)
        except Exception as e:
            logger.debug("native_reader_thread error: %s", e)
            break


def start_native_bridge():
    """Launch reader and writer threads for Chrome Native Messaging."""
    writer = threading.Thread(target=native_writer_thread, daemon=True, name="nm-writer")
    reader = threading.Thread(target=native_reader_thread, daemon=True, name="nm-reader")
    writer.start()
    reader.start()
    return writer, reader
