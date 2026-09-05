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
	"""Read until \r\n\r\n. Return (method, url, headers_dict, body_prefix_bytes) or (None,None,None,None).
	Returns ('TOO_LARGE', raw_data, None, None) when header exceeds limit."""
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
		if len(data) > 262144:  # 256KB — LLM requests can have large auth headers
			logger.warning("HTTP header too large: %d bytes (limit 256KB), returning 431", len(data))
			# Drain remaining header data until \r\n\r\n
			while b"\r\n\r\n" not in data:
				try:
					chunk = sock.recv(4096)
				except Exception:
					return 'TOO_LARGE', data[:262144], None, None
				if not chunk:
					return 'TOO_LARGE', data[:262144], None, None
				data += chunk
				if len(data) > 1073741824:  # 1MB safety valve
					return 'TOO_LARGE', data[:262144], None, None
			return 'TOO_LARGE', data[:262144], None, None

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
	"""Read exact content_length bytes from socket. Returns (body, ok) — ok=False means truncation."""
	body = body_prefix
	remaining = content_length - len(body_prefix)
	total_read = len(body)
	while remaining > 0:
		try:
			chunk = sock.recv(min(65536, remaining))
		except socket.timeout:
			logger.warning("BODY_READ_TIMEOUT: read=%d expected=%d remaining=%d", total_read, content_length, remaining)
			return body, False
		except Exception as e:
			logger.warning("BODY_READ_ERROR: read=%d expected=%d remaining=%d err=%s", total_read, content_length, remaining, e)
			return body, False
		if not chunk:
			logger.warning("BODY_READ_EOF: read=%d expected=%d remaining=%d", total_read, content_length, remaining)
			return body, False
		body += chunk
		remaining -= len(chunk)
		total_read += len(chunk)
	logger.debug("BODY_READ_OK: read=%d expected=%d", total_read, content_length)
	return body[:content_length], True


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

	body_len = len(body) if body else 0
	if body_len > 0:
		logger.debug("NM_FWD: id=%d %s %s body=%d bytes", req_id, method, url, body_len)
	else:
		logger.debug("NM_FWD: id=%d %s %s", req_id, method, url)

	# Response collection
	resp_event = threading.Event()
	end_event = threading.Event()
	resp_data = {
		"status": 502, "statusText": "Bad Gateway",
		"headers": {}, "chunks": [], "error": None
	}
	stale = [False]  # mutable flag — True if timeout already fired

	def handler(msg):
		mtype = msg.get("type", "")
		mid = msg.get("id")
		if mid != req_id:
			return
		if stale[0]:
			logger.debug("NM late msg dropped: id=%d type=%s (request already timed out)", req_id, mtype)
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
		utils.nm_send_msg({"type": "request_start", "id": req_id, "method": method, "url": url, "headers": clean_headers})

		# Send body in chunks — 256KB max to stay safely under Chrome NM 1MB limit
		# (256KB binary → 341KB base64 → ~350KB JSON with overhead, well under 1MB)
		if body:
			chunk_max = 256 * 1024  # 256KB
			total_sent = 0
			for offset in range(0, len(body), chunk_max):
				chunk = body[offset:offset + chunk_max]
				utils.nm_send_msg({
					"type": "request_chunk",
					"id": req_id,
					"data": base64.b64encode(chunk).decode("ascii")
				})
				total_sent += len(chunk)
			logger.debug("NM_BODY_SENT: id=%d total=%d bytes in %d chunks", req_id, total_sent, (len(body) + chunk_max - 1) // chunk_max)

		# Send request_end
		utils.nm_send_msg({"type": "request_end", "id": req_id})

		# Wait for response headers (LLM APIs can take >60s for large payloads)
		if not resp_event.wait(timeout=180):
			stale[0] = True
			raise Exception("NM response timeout (180s)")
		if resp_data["error"]:
			raise Exception(f"NM error: {resp_data['error']}")

		# Build response head with Set-Cookie support
		resp_headers = resp_data["headers"]
		drop_resp = {"connection", "proxy-connection", "keep-alive", "content-length", "transfer-encoding", "content-encoding"}
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
		logger.debug("NM_FWD_DONE: id=%d status=%d", req_id, resp_data["status"])

	except Exception as e:
		logger.warning("NM_FWD_FAIL: id=%d %s %s err=%s", req_id, method, url, e)
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

	body_len = len(body) if body else 0
	logger.debug("URL_FWD: %s %s body=%d bytes", method, url, body_len)

	data = body if body else None
	req = urllib.request.Request(url, data=data, headers=clean_headers, method=method)

	try:
		resp = urllib.request.urlopen(req, timeout=30)
	except urllib.error.HTTPError as e:
		resp = e
	except Exception as e:
		logger.warning("URL_FWD_FAIL: %s %s err=%s", method, url, e)
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
	logger.debug("URL_FWD_DONE: %s %s status=%d resp_body=%d", method, url, status, len(body_bytes))

	try:
		sock.sendall(head + body_bytes)
	except Exception as e:
		logger.debug("_forward_via_urllib send response error: %s", e)


def handle_http_request(sock, method, url, headers, body_prefix, host, port):
	"""Main HTTP handler: read body, determine URL, forward via NM or urllib."""

	# Reject self-referencing requests (someone proxying the proxy itself)
	if host in ("127.0.0.1", "localhost", "::1") and port == utils.LOCAL_PROXY_PORT:
		logger.debug("REJECTED self-reference: %s %s:%d (proxy cannot call itself)", method, host, port)
		err_body = b"403 Forbidden: proxy cannot call itself"
		err = _build_response_head(403, "Forbidden", {}, len(err_body))
		try:
			sock.sendall(err + err_body)
		except Exception:
			pass
		return

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
		body, body_ok = _read_content_length_body(sock, body_prefix, content_length)
		if not body_ok:
			logger.warning("HTTP_REQ_BODY_TRUNC: %s %s got=%d expected=%d", method, url, len(body), content_length)
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
	"""
	try:
		client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
	except Exception:
		return

	_connect_mitm(client_sock, host, port, force_urllib=not utils.CHROME_CONNECTED)


def _connect_mitm(client_sock, host, port, force_urllib=False):
	"""Wrap client socket as TLS server using per-host cert, then run MITM loop."""
	host_clean = host.split(":")[0]
	cert_path, key_path = utils.CertManager.get_cert_for_host(host_clean)

	ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	ssl_context.load_cert_chain(cert_path, key_path)
	# Advertise HTTP/1.1 only — no h2 ALPN to prevent HTTP/2 negotiation
	ssl_context.set_alpn_protocols(['http/1.1'])

	try:
		tls_sock = ssl_context.wrap_socket(client_sock, server_side=True)
	except Exception as e:
		logger.debug("MITM TLS handshake failed for %s: %s", host, e)
		return

	try:
		tls_sock.settimeout(30)
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
	All traffic through CONNECT is HTTPS — the proxy terminated TLS."""
	max_requests = 100
	for _ in range(max_requests):
		method, url, headers, body_prefix = _read_http_header(tls_sock)
		if method == 'TOO_LARGE':
			err_body = b"431 Request Header Fields Too Large"
			err_head = _build_response_head(431, "Request Header Fields Too Large", {}, len(err_body))
			try:
				tls_sock.sendall(err_head + err_body)
			except Exception:
				pass
			break
		if method is None:
			break

		body = body_prefix
		transfer_encoding = headers.get("Transfer-Encoding", "").lower()
		content_length_raw = headers.get("Content-Length")

		if transfer_encoding == "chunked":
			body = _read_chunked_body(tls_sock, body_prefix)
		elif content_length_raw is not None:
			try:
				content_length = int(content_length_raw)
			except ValueError:
				content_length = 0
			body, body_ok = _read_content_length_body(tls_sock, body_prefix, content_length)
			if not body_ok:
				logger.warning("MITM_BODY_TRUNC: %s %s got=%d expected=%d", method, url, len(body), content_length)
				err_body = b"Request body truncated"
				err_head = _build_response_head(400, "Bad Request", {}, len(err_body))
				try:
					tls_sock.sendall(err_head + err_body)
				except Exception:
					pass
				break
		else:
			body = body_prefix if body_prefix else b""

		# CONNECT → MITM → always https
		if url.startswith("http://") or url.startswith("https://"):
			full_url = url
		else:
			if port == 443:
				full_url = "https://%s%s" % (host, url)
			else:
				full_url = "https://%s:%d%s" % (host, port, url)

		if force_urllib:
			_forward_via_urllib(tls_sock, method, full_url, headers, body)
		else:
			_forward_via_nm(tls_sock, method, full_url, headers, body)

		# Honour client's Connection: close
		if headers.get("Connection", "").lower() == "close":
			break


def handle_client(client_sock):
	"""Entry point for each connection."""
	try:
		client_sock.settimeout(30)

		method, url, headers, body_prefix = _read_http_header(client_sock)
		if method == 'TOO_LARGE':
			err_body = b"431 Request Header Fields Too Large"
			err = _build_response_head(431, "Request Header Fields Too Large", {}, len(err_body))
			try:
				client_sock.sendall(err + err_body)
			except Exception:
				pass
			client_sock.close()
			return
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
	"""Bind socket and accept loop. Exits immediately if port is in use (prevents multiple instances)."""
	bind_addr = (utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
	server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
	try:
		server_sock.bind(bind_addr)
	except OSError as e:
		logger.warning("Port %s:%d already in use — proxy already running. Exiting.", utils.LOCAL_PROXY_IP, utils.LOCAL_PROXY_PORT)
		try:
			server_sock.close()
		except Exception:
			pass
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
			msg_type = msg.get("type", "?")
			json_data = json.dumps(msg, ensure_ascii=False)
			json_bytes = json_data.encode("utf-8")
			msg_len = len(json_bytes)
			if msg_len > 900 * 1024:
				logger.warning("NM_MSG_LARGE: type=%s id=%s size=%d (near Chrome 1MB limit)", msg_type, msg.get("id", "?"), msg_len)
			length_bytes = struct.pack("<I", msg_len)
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
				logger.warning("NM stdin truncated: expected=%d got=%d", msg_length, len(json_bytes) if json_bytes else 0)
				break
			msg = json.loads(json_bytes.decode("utf-8", errors="replace"))
			# Route by id — do NOT broadcast to all handlers
			msg_id = msg.get("id")
			if msg_id is not None and msg_id in utils.nm_pending_requests:
				try:
					utils.nm_pending_requests[msg_id](msg)
				except Exception as e:
					logger.debug("NM handler error: %s", e)
			else:
				# Log unexpected messages (ping, or stale id)
				msg_type = msg.get("type", "?")
				if msg_type != "ping":
					logger.debug("NM unhandled message: type=%s id=%s", msg_type, msg_id)
	except Exception as e:
		logger.debug("native_reader_thread error: %s", e)
	finally:
		utils.CHROME_CONNECTED = False
		logger.warning("Chrome extension disconnected — shutting down")
		os._exit(0)


def start_native_bridge():
	"""Launch reader and writer threads for Chrome Native Messaging."""
	writer = threading.Thread(target=native_writer_thread, daemon=True, name="nm-writer")
	reader = threading.Thread(target=native_reader_thread, daemon=True, name="nm-reader")
	writer.start()
	reader.start()
	return writer, reader
