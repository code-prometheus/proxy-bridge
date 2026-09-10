def _forward_via_nm(sock, method, url, headers, body):
\t"""Forward request through Chrome NM with Range-based resume on failure.
\t
\tUses inner _nm_fetch() helper to send one NM request and collect
\tthe full result. On partial failure for GET with no body, retries
\twith Range: bytes={received}- to resume up to 2 more times.
\t"""
\t# ---- Build clean request headers (filter Chrome-forbidden) ----
\tdrop_request = {"connection", "proxy-connection", "keep-alive", "host",
\t\t"content-length", "transfer-encoding", "content-encoding", "accept-encoding"}
\tclean_headers = {}
\tfor k, v in headers.items():
\t\tkl = k.lower()
\t\tif kl not in drop_request:
\t\t\tclean_headers[k] = v

\tif body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
\t\tce_key = NoneA
\t\tfor k in headers:
\t\t\tif k.lower() == 'content-encoding':
\t\t\t\tce_key = k
\t\t\t\tbreak
\t\tif ce_key is None:
\t\t\tclean_headers['Content-Encoding'] = 'gzip'
\t\t\tlogger.debug('NM_GZIP_AUTO: added Content-Encoding: gzip for %d-byte body', len(body))

\t# ---- Inner NM fetch helper (always Chrome fetch(), never urllib) ----
\tdef _nm_fetch(req_headers, req_body):
\t\t"""Send one NM request. Returns (status, stext, headers_dict, body_bytes, error_or_None)."""
\t\twith utils.nm_lock:
\t\t\trid = utils.nm_request_id_counter
\t\t\tutils.nm_request_id_counter += 1

\t\tre = threading.Event()
\t\tee = threading.Event()
\t\trd = {"status": 502, "statusText": "Bad Gateway", "headers": {}, "chunks": [], "error": None}

\t\tdef _h(msg):
\t\t\tif msg.get("id") != rid:
\t\t\t\treturn
\t\t\tmtype = msg.get("type", "")
\t\t\tif mtype == "response":
\t\t\t\trd["status"] = msg.get("status", 200)
\t\t\t\trd["statusText"] = msg.get("statusText", "OK")
\t\t\t\trd["headers"] = msg.get("headers", {})
\t\t\t\tre.set()
\t\t\telif mtype == "chunk":
\t\t\t\tb64 = msg.get("data", "")
\t\t\t\tif b64:
\t\t\t\t\trd["chunks"].append(base64.b64decode(b64))
\t\t\telif mtype == "end":
\t\t\t\tee.set()
\t\t\telif mtype == "error":
\t\t\t\trd["error"] = msg.get("error", "Unknown error")
\t\t\t\tre.set()
\t\t\t\tee.set()

\t\tutils.nm_pending_requests[rid] = _h
\t\ttry:
\t\t\tutils.nm_send_msg({"type": "request_start", "id": rid, "method": method, "url": url,
\t\t\t\t"headers": req_headers})
\t\t\tif req_body:
\t\t\t\tfor off in range(0, len(req_body), 512 * 1024):
\t\t\t\t\tc = req_body[off:off + 512 * 1024]
\t\t\t\t\tutils.nm_send_msg({"type": "request_chunk", "id": rid,
\t\t\t\t\t\t"data": base64.b64encode(c).decode("ascii")})
\t\t\tutils.nm_send_msg({"type": "request_end", "id": rid})

\t\t\tif not re.wait(timeout=120):
\t\t\t\treturn (502, "Gateway Timeout", {}, b"", "NM response timeout (120s)")
\t\t\tif rd["error"]:
\t\t\t\tbody_bytes = b"".join(rd["chunks"])
\t\t\t\treturn (rd["status"], rd["statusText"], rd["headers"], body_bytes, rd["error"])

\t\t\tif not ee.wait(timeout=600):
\t\t\t\tbody_bytes = b"".join(rd["chunks"])
\t\t\t\treturn (rd["status"], rd["statusText"], rd["headers"], body_bytes,
\t\t\t\t\t"Partial: %d bytes, NM body timeout (600s)" % len(body_bytes))

\t\t\tbody_bytes = b"".join(rd["chunks"])
\t\t\treturn (rd["status"], rd["statusText"], rd["headers"], body_bytes, None)
\t\texcept Exception as e:
\t\t\tbody_bytes = b"".join(rd["chunks"])
\t\t\treturn (502, "Bad Gateway", {}, body_bytes, str(e))
\t\tfinally:
\t\t\tutils.nm_pending_requests.pop(rid, None)

\t# ---- Main flow: call _nm_fetch, retry with Range on partial failure ----
\ttry:
\t\tstatus, stext, resp_headers, body_bytes, error = _nm_fetch(clean_headers, body)

\t\t# Range-based resume: GET + no body + got some bytes + error
\t\tif error and method == "GET" and not body and len(body_bytes) > 0:
\t\t\ttotal = len(body_bytes)
\t\t\tlogger.debug("NM_RETRY: got %d bytes, error=%s -- retrying with Range", total, error[:80] if error else "")
\t\t\tfor attempt in range(2):
\t\t\t\trng_headers = dict(clean_headers)
\t\t\t\trng_headers["Range"] = "bytes=%d-" % total
\t\t\t\t_, _, _, b2, e2 = _nm_fetch(rng_headers, None)
\t\t\t\tlogger.debug("NM_RETRY: attempt %d got %d bytes, err=%s",
\t\t\t\t\tattempt + 1, len(b2), (e2 or "none")[:80])
\t\t\t\tbody_bytes += b2
\t\t\t\ttotal = len(body_bytes)
\t\t\t\tif not e2:
\t\t\t\t\terror = None
\t\t\t\t\tbreak
\t\t\tlogger.debug("NM_RESUME_DONE: total=%d bytes, final_err=%s",
\t\t\t\tlen(body_bytes), error or "none")

\t\t# Total failure: zero bytes + error -> 502
\t\tif error and len(body_bytes) == 0:
\t\t\traise Exception(error)

\t\t# ---- Send HTTP response to client ----
\t\tdrop_resp = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "content-encoding"}
\t\thead = "HTTP/1.1 %d %s\r\n" % (status, stext)
\t\tfor k, v in resp_headers.items():
\t\t\tkl = k.lower()
\t\t\tif kl == "set-cookie" and isinstance(v, list):
\t\t\t\tfor cv in v:
\t\t\t\t\thead += "Set-Cookie: %s\r\n" % cv
\t\t\telif kl not in drop_resp:
\t\t\t\thead += "%s: %s\r\n" % (k, v)
\t\thead += "Content-Length: %d\r\n" % len(body_bytes)
\t\thead += "Connection: close\r\n\r\n"
\t\tsock.sendall(head.encode("utf-8"))

\t\tfor i in range(0, len(body_bytes), 4 * 1024 * 1024):
\t\t\tsock.sendall(body_bytes[i:i + 4 * 1024 * 1024])
\t\tlogger.debug("NM_FINAL: status=%d body=%d err=%s", status, len(body_bytes), error or "none")

\texcept Exception as e:
\t\tlogger.debug("_forward_via_nm error: %s", e)
\t\ttry:
\t\t\tsock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
\t\texcept Exception:
\t\t\tpass
