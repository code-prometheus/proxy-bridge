import base64
import json
import logging
import os
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

logger = logging.getLogger('proxy_bridge.local_proxy')

proxy_executor = ThreadPoolExecutor(max_workers=500)


def _read_http_header(sock):
	"""Read until \\r\\n\\r\\n. Return (method, url, headers_dict, body_prefix_bytes) or (None,None,None,None)."""
	data = b""
	first = True
	while b"\r\n\r\n" not in data and b"\n\n" not in data:
		try:
			chunk = sock.recv(4096)
		except Exception as e:
			logger.debug("HEADER_RECV_ERR: %s", e)
			return None, None, None, None
		if not chunk:
			logger.debug("HEADER_RECV_EOF: read=%d", len(data))
			return None, None, None, None
		data += chunk
		if first and len(data) >= 40:
			first = False
			# dump raw data: printable chars + hex for non-printable
			printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:200])
			logger.debug("HEADER_RAW_DUMP: printable=[%s]", printable)
			logger.debug("HEADER_RAW_DUMP: hex=%s", data[:200].hex())
		if len(data) > 65536:
			logger.warning("HEADER_TOO_LARGE: len=%d hex=%s", len(data), data[:200].hex())
			return 'TOO_LARGE', data[:65536], None, None

	header_end = data.find(b"\r\n\r\n")
	if header_end >= 0:
		header_bytes = data[:header_end]
		body_prefix = data[header_end + 4:]
		sep = "\r\n"
	else:
		header_end = data.find(b"\n\n")
		header_bytes = data[:header_end]
		body_prefix = data[header_end + 2:]
		sep = "\n"

	header_text = header_bytes.decode("utf-8", errors="replace")
	lines = header_text.split(sep)

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
			headers[key.strip()] = value.strip()

	logger.debug("HEADER_KEYS: %s te=%s cl=%s ce=%s", sorted(headers.keys()), _hdr(headers,"Transfer-Encoding"), _hdr(headers,"Content-Length"), _hdr(headers,"Content-Encoding"))
	return method, url, headers, body_prefix


def _hdr(headers, key):
	"""Case-insensitive header lookup."""
	kl = key.lower()
	for k, v in headers.items():
		if k.lower() == kl:
			return v
	return ""


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
				time.sleep(0.05)
				continue
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
					time.sleep(0.05)
					continue
				data += chunk
			return body

		while len(data) < chunk_size + 2:
			try:
				chunk = sock.recv(max(4096, chunk_size + 2 - len(data)))
			except Exception as e:
				logger.debug("_read_chunked_body chunk data recv error: %s", e)
				return body
			if not chunk:
				time.sleep(0.05)
				continue
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
			time.sleep(0.05)
			continue
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
    """Forward through Chrome NM with size-driven Range resume."""
    clean_headers = {}
    drop = {"connection", "proxy-connection", "keep-alive", "host"}
    for k, v in headers.items():
        kl = k.lower()
        if kl not in drop:
            clean_headers[k] = v

    if body and len(body) >= 2 and body[:2] == b"\x1f\x8b":
        for k in headers:
            if k.lower() == "content-encoding":
                clean_headers["Content-Encoding"] = "gzip"
                break

    # ---- NM fetch helper ----
    def _nm_fetch(hdrs, bd):
        with utils.nm_lock:
            r = utils.nm_request_id_counter
            utils.nm_request_id_counter += 1
        re = threading.Event(); ee = threading.Event()
        rd = {"status": 502, "statusText": "Bad Gateway", "headers": {}, "chunks": [], "done": False}
        def _h(msg):
            if msg.get("id") != r: return
            mt = msg.get("type", "")
            if mt == "response":
                rd["status"] = msg.get("status", 200)
                rd["statusText"] = msg.get("statusText", "OK")
                rd["headers"] = msg.get("headers", {})
                re.set()
            elif mt == "chunk":
                b64 = msg.get("data", "")
                if b64: rd["chunks"].append(base64.b64decode(b64))
            elif mt == "end":
                rd["done"] = True
                ee.set()
            elif mt == "error":
                rd["done"] = False
                re.set(); ee.set()
        utils.nm_pending_requests[r] = _h
        utils.nm_send_msg({"type": "request_start", "id": r, "method": method, "url": url, "headers": hdrs})
        if bd:
            for off in range(0, len(bd), 512*1024):
                c = bd[off:off+512*1024]
                utils.nm_send_msg({"type": "request_chunk", "id": r, "data": base64.b64encode(c).decode("ascii")})
        utils.nm_send_msg({"type": "request_end", "id": r})
        return r, re, ee, rd

    # ---- Stream helper ----
    def _stream(re, ee, rd):
        idx = 0; dl = time.time() + 600
        while not ee.is_set() or idx < len(rd["chunks"]):
            while idx < len(rd["chunks"]):
                try: sock.sendall(rd['chunks'][idx])
                except Exception: ee.set(); break
                idx += 1
            if ee.is_set() and idx >= len(rd["chunks"]): break
            ee.wait(0.1)
            if time.time() > dl: break
        return sum(len(c) for c in rd["chunks"][:idx])

    try:
        # Phase 1: first request
        rid, re, ee, rd = _nm_fetch(clean_headers, body)
        if not re.wait(timeout=120):
            raise Exception("NM timeout")

        # Get expected size
        upstream_cl = _hdr(rd["headers"], "Content-Length") or "0"
        expected = int(upstream_cl) if upstream_cl.isdigit() else 0

        # Phase 2: send head immediately
        drop_r = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "content-encoding"}
        h = f"HTTP/1.1 {rd['status']} {rd['statusText']}\\r\\n"
        for k, v in rd['headers'].items():
            kl = k.lower()
            if kl == "set-cookie" and isinstance(v, list):
                for cv in v: h += f"Set-Cookie: {cv}\\r\\n"
            elif kl not in drop_r: h += f"{k}: {v}\\r\\n"
        h += "Connection: close\\r\\n\\r\\n"
        sock.sendall(h.encode("utf-8"))

        # Phase 3: stream + resume loop
        total = _stream(re, ee, rd)
        utils.nm_pending_requests.pop(rid, None)

        if not expected:
            if rd.get("done") and method == "GET" and not body:
                # fully received via chunked encoding, no resume needed
                expected = total
            elif method == "GET" and not body:
                # chunked not yet complete, resume with Range
                expected = 10 * 1024 * 1024 * 1024 # 10GB cap

        while total < expected and method == "GET" and not body:
            logger.debug("NM_RESUME: have=%d need=%d", total, expected)
            rng = dict(clean_headers)
            rng["Range"] = "bytes=%d-" % total
            r2, e2, ee2, rd2 = _nm_fetch(rng, None)
            if not e2.wait(timeout=120):
                utils.nm_pending_requests.pop(r2, None); time.sleep(2); continue
            # Update expected from Range response Content-Length if available
            cl2 = _hdr(rd2["headers"], "Content-Length") or ""
            cl2n = int(cl2) if cl2.isdigit() else 0
            if cl2n > 0:
                expected = total + cl2n
            n = _stream(e2, ee2, rd2)
            total += n
            utils.nm_pending_requests.pop(r2, None)
            if rd2.get("done") and total >= expected:
                break
            if n == 0: time.sleep(2)
        logger.debug("NM_DONE: total=%d", total)

    except Exception as e:
        logger.debug("_forward_via_nm err: %s", e)
        try: sock.sendall(b'HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n')
        except Exception: pass
def _forward_via_urllib(sock, method, url, headers, body):
	"""Fallback: use urllib for direct HTTP request."""
	clean_headers = {}
	drop_request = {"connection", "proxy-connection", "keep-alive", "host"}
	for k, v in headers.items():
		kl = k.lower()
		if kl not in drop_request:
			clean_headers[k] = v

	# Auto-detect gzip body: if body starts with 1f 8b and no Content-Encoding,
	# add it so the upstream server knows to decompress
	if body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
		ce_key = None
		for k in headers:
			if k.lower() == 'content-encoding':
				ce_key = k
				break
		if ce_key is None:
			clean_headers['Content-Encoding'] = 'gzip'
			logger.debug("NM_GZIP_AUTO: added Content-Encoding: gzip for %d-byte body", len(body))

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
	transfer_encoding = _hdr(headers, "Transfer-Encoding").lower()
	content_length_raw = _hdr(headers, "Content-Length") or None

	if headers and transfer_encoding == "chunked":
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


# ===========================================================================
# CONNECT MITM: Terminate TLS at proxy, decrypt HTTP, forward via Chrome NM
# ===========================================================================

def handle_connect_tunnel(client_sock, host, port):
	"""CONNECT MITM: terminate TLS at proxy, decrypt HTTP, forward via Chrome NM.

	Proxy is the TLS endpoint — client never reaches the real server.
	All upstream SSL errors are absorbed by Chrome's fetch stack (ghelper).

	Flow:
	1. Send 200 Connection Established → client thinks tunnel is open
	2. Wrap socket with per-host TLS server certificate (signed by local CA)
	3. Read decrypted HTTP request from TLS socket
	4. Forward method/url/headers/body to Chrome NM (or urllib fallback)
	5. Write response back through TLS socket
	6. Loop for HTTP keep-alive
	"""
	try:
		client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
	except Exception:
		return

	_connect_mitm(client_sock, host, port, force_urllib=not utils.CHROME_CONNECTED)


def _connect_mitm(client_sock, host, port, force_urllib=False):
	"""Wrap client socket as TLS server using per-host cert, then run MITM loop.

	Uses per-host certificates signed by the local CA. Client must trust
	the CA (run --install-ca once as Admin) to avoid certificate errors.
	"""
	host_clean = host.split(":")[0]
	cert_path, key_path = utils.CertManager.get_cert_for_host(host_clean)

	ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	ssl_context.load_cert_chain(cert_path, key_path)

	try:
		tls_sock = ssl_context.wrap_socket(client_sock, server_side=True)
	except Exception as e:
		logger.debug("MITM TLS handshake failed for %s: %s", host, e)
		return

	try:
		tls_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		tls_sock.settimeout(300)  # large downloads can take minutes
		_mitm_loop(tls_sock, host, port, force_urllib)
	except Exception as e:
		logger.debug("MITM loop error for %s: %s", host, e)
	finally:
		try:
			tls_sock.close()
		except Exception:
			pass


def _mitm_loop(tls_sock, host, port, force_urllib=False):
	"""Decrypt HTTP requests from TLS socket, forward each to Chrome NM or urllib.

	Supports HTTP keep-alive: reads multiple request/response pairs until
	the client disconnects or sends Connection: close.
	"""
	max_requests = 100
	for _ in range(max_requests):
		method, url, headers, body_prefix = _read_http_header(tls_sock)
		if method is None:
			break

		body = body_prefix
		transfer_encoding = _hdr(headers, "Transfer-Encoding").lower()
		content_length_raw = _hdr(headers, "Content-Length") or None
		logger.debug("MITM_BODY_START: body_pre=%d te=%s cl=%s", len(body_prefix), transfer_encoding, content_length_raw)

		if transfer_encoding == "chunked":
			body = _read_chunked_body(tls_sock, body_prefix)
		elif content_length_raw:
			try:
				content_length = int(content_length_raw)
			except ValueError:
				content_length = 0
			body = _read_content_length_body(tls_sock, body_prefix, content_length)
		else:
			body = body_prefix if body_prefix else b""

		# Build absolute URL — everything is https over CONNECT
		scheme = "https" if port == 443 else "http"
		if url.startswith("http://") or url.startswith("https://"):
			full_url = url
		elif (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
			full_url = "%s://%s%s" % (scheme, host, url)
		else:
			full_url = "%s://%s:%d%s" % (scheme, host, port, url)

		logger.debug("MITM request: %s %s", method, full_url)

		logger.debug("MITM_FWD_CALL: body=%d", len(body))
		if force_urllib:
			_forward_via_urllib(tls_sock, method, full_url, headers, body)
		else:
			_forward_via_nm(tls_sock, method, full_url, headers, body)

		# Honour client's Connection: close
		if headers and _hdr(headers, "Connection").lower() == "close":
			break


def handle_client(client_sock):
	"""Entry point for each connection."""
	try:
		client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		client_sock.settimeout(300)  # large downloads can take minutes

		method, url, headers, body_prefix = _read_http_header(client_sock)
		if method is None:
			client_sock.close()
			return

		host_header = _hdr(headers, "Host")
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
	server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
	for _ in range(5):
		try:
			server_sock.bind(bind_addr)
			break
		except OSError:
			time.sleep(2)
	else:
		os._exit(0)
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
		logger.warning("Chrome disconnected - proxy stays alive, urllib fallback active")
		# Fail all pending NM requests so clients do not hang
		for rid in list(utils.nm_pending_requests.keys()):
			try:
				utils.nm_pending_requests[rid]({"type": "error", "id": rid, "error": "NM disconnected"})
			except Exception:
				pass
		utils.nm_pending_requests.clear()


def start_native_bridge():
	"""Launch reader and writer threads for Chrome Native Messaging."""
	writer = threading.Thread(target=native_writer_thread, daemon=True, name="nm-writer")
	reader = threading.Thread(target=native_reader_thread, daemon=True, name="nm-reader")
	writer.start()
	reader.start()
	return writer, reader
