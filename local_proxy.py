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

    # Generate unique request ID
    with utils.nm_lock:
        req_id = utils.nm_request_id_counter
        utils.nm_request_id_counter += 1

    # Response collection
    resp_event = threading.Event()
    end_event = threading.Event()
    resp_data = {
        "status": 502, "statusText": "Bad Gateway",
        "headers": {}, "chunks": [], "error": None
    }

    def handler(msg):
        mtype = msg.get("type", "")
        mid = msg.get("id")
        if mid != req_id:
            return
        if mtype == "response":
            resp_data["status"] = msg.get("status", 200)
            resp_data["statusText"] = msg.get("statusText", "OK")
            resp_data["headers"] = msg.get("headers", {})
            resp_event.set()
        elif mtype == "chunk":
            b64 = msg.get("data", "")
            if b64:
                resp_data["chunks"].append(base64.b64decode(b64))
        elif mtype == "end":
            end_event.set()
        elif mtype == "error":
            resp_data["error"] = msg.get("error", "Unknown error")
            resp_event.set()
            end_event.set()

    utils.nm_pending_requests[req_id] = handler

    try:
        # Send request_start with id
        utils.nm_send_msg({
            "type": "request_start",
            "id": req_id,
            "method": method,
            "url": url,
            "headers": clean_headers
        })

        # Send body in chunks
        if body:
            chunk_max = 512 * 1024  # 512KB
            for offset in range(0, len(body), chunk_max):
                chunk = body[offset:offset + chunk_max]
                utils.nm_send_msg({
                    "type": "request_chunk",
                    "id": req_id,
                    "data": base64.b64encode(chunk).decode("ascii")
                })

        # Send request_end
        utils.nm_send_msg({"type": "request_end", "id": req_id})

        # Wait for response headers
        if not resp_event.wait(timeout=30):
            raise Exception("NM response timeout")
        if resp_data["error"]:
            raise Exception(f"NM error: {resp_data['error']}")

        # Build response head with Set-Cookie support
        resp_headers = resp_data["headers"]
        drop_resp = {"connection", "proxy-connection", "keep-alive",
                     "content-length", "transfer-encoding", "content-encoding"}
        head = f"HTTP/1.1 {resp_data['status']} {resp_data['statusText']}\r\n"
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "set-cookie" and isinstance(v, list):
                for cv in v:
                    head += f"Set-Cookie: {cv}\r\n"
            elif kl not in drop_resp:
                head += f"{k}: {v}\r\n"
        head += "Transfer-Encoding: chunked\r\n"
        head += "Connection: close\r\n\r\n"
        sock.sendall(head.encode("utf-8"))

        # Stream body chunks
        # Wait for end_event with periodic checks for new chunks
        last_chunk_count = 0
        while not end_event.is_set():
            end_event.wait(0.1)
            if len(resp_data["chunks"]) > last_chunk_count:
                for chunk_bytes in resp_data["chunks"][last_chunk_count:]:
                    chunk_header = f"{len(chunk_bytes):X}\r\n".encode("utf-8")
                    sock.sendall(chunk_header + chunk_bytes + b"\r\n")
                last_chunk_count = len(resp_data["chunks"])

        # Send final chunk end marker
        sock.sendall(b"0\r\n\r\n")

    except Exception as e:
        logger.debug("_forward_via_nm error: %s", e)
        try:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
    finally:
        utils.nm_pending_requests.pop(req_id, None)


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
    """CONNECT tunnel: transparent TLS relay with Chrome NM priority.

    When Chrome NM is connected (ghelper active):
        Client TLS data → forwarded through Chrome's network stack.
    When Chrome NM disconnected (fallback):
        Client TLS data → direct TCP connection to target.

    Proxy does NOT MITM — TLS is between client and target.
    Chrome NM path uses ghelper for unfiltered internet access.
    """
    try:
        client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    except Exception:
        return

    if utils.CHROME_CONNECTED:
        _tunnel_via_nm(client_sock, host, port)
    else:
        _tunnel_via_direct(client_sock, host, port)


def _tunnel_via_nm(client_sock, host, port):
    """Create a TCP tunnel through Chrome NM to the remote host."""
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.settimeout(10)
    try:
        target.connect((host, port))
    except Exception as e:
        logger.debug("_tunnel_via_nm connect error: %s", e)
        target.close()
        return

    # Bidirectional pump
    t1 = threading.Thread(target=_pump, args=(client_sock, target), daemon=True)
    t2 = threading.Thread(target=_pump, args=(target, client_sock), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=5)
    try:
        client_sock.close()
    except Exception:
        pass
    try:
        target.close()
    except Exception:
        pass


def _tunnel_via_direct(client_sock, host, port):
    """Try direct TCP connection for CONNECT tunnel (fallback without Chrome)."""
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.settimeout(10)
    try:
        target.connect((host, port))
    except Exception as e:
        logger.debug("_tunnel_via_direct connect to %s:%s failed: %s", host, port, e)
        try:
            client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
        target.close()
        return

    _pump_bidirectional(client_sock, target)


def _pump(src, dst):
    """Copy data from src to dst."""
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass


def _pump_bidirectional(a, b):
    """Bidirectional byte pump between two sockets."""
    t1 = threading.Thread(target=_pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=_pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    # Wait for either side to finish
    t1.join(timeout=120)
    t2.join(timeout=5)
    try:
        a.close()
    except Exception:
        pass
    try:
        b.close()
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
            utils.original_stdout_buffer.write(length_bytes + json_bytes)
            utils.original_stdout_buffer.flush()
        except Exception as e:
            logger.debug("native_writer_thread error: %s", e)
            break


def native_reader_thread():
    """Read length-prefixed JSON from stdin, route to utils.nm_pending_requests."""
    utils.CHROME_CONNECTED = True
    logger.info("Chrome extension connected via Native Messaging")
    try:
        while True:
            raw_length = sys.stdin.buffer.read(4)
            if not raw_length or len(raw_length) < 4:
                logger.warning("NM stdin EOF — Chrome disconnected")
                break
            msg_length = struct.unpack("<I", raw_length)[0]
            if msg_length > 16 * 1024 * 1024:
                logger.warning("NM message too large: %d bytes, skipping", msg_length)
                continue
            json_bytes = sys.stdin.buffer.read(msg_length)
            if not json_bytes or len(json_bytes) < msg_length:
                break
            msg = json.loads(json_bytes.decode("utf-8", errors="replace"))
            # Route to registered handler (filtered by id inside handler)
            for handler in list(utils.nm_pending_requests.values()):
                try:
                    handler(msg)
                except Exception as e:
                    logger.debug("NM handler error: %s", e)
    except Exception as e:
        logger.debug("native_reader_thread error: %s", e)
    finally:
        utils.CHROME_CONNECTED = False
        logger.warning("Chrome extension disconnected — falling back to urllib")


def start_native_bridge():
    """Launch reader and writer threads for Chrome Native Messaging."""
    writer = threading.Thread(target=native_writer_thread, daemon=True, name="nm-writer")
    reader = threading.Thread(target=native_reader_thread, daemon=True, name="nm-reader")
    writer.start()
    reader.start()
    return writer, reader
