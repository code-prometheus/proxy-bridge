"""Fix _forward_via_nm: send head immediately, stream body, with Range resume."""
PATH = "D:/proxy-bridge/local_proxy.py"
import py_compile

with open(PATH, "rb") as f:
    data = f.read()

fn = data.index(b"def _forward_via_nm(")
fn_end = data.index(b"\r\ndef _forward_via_urllib(", fn) + 2

new_fn = b'''def _forward_via_nm(sock, method, url, headers, body):
    """Forward through Chrome NM: immediate head, stream body, Range resume."""
    clean_headers = {}
    drop_request = {"connection", "proxy-connection", "keep-alive", "host"}
    for k, v in headers.items():
        kl = k.lower()
        if kl not in drop_request:
            clean_headers[k] = v

    if body and len(body) >= 2 and body[:2] == b'\\x1f\\x8b':
        ce_key = None
        for k in headers:
            if k.lower() == 'content-encoding':
                ce_key = k
                break
        if ce_key is None:
            clean_headers['Content-Encoding'] = 'gzip'
            logger.debug("NM_GZIP_AUTO: gzip %d-byte body", len(body))

    # ---- Inner: dispatch one NM request, register handler ----
    def _nm_dispatch(req_headers, req_body):
        """Register NM request and return (rid, resp_event, end_event, resp_data)."""
        with utils.nm_lock:
            rid = utils.nm_request_id_counter
            utils.nm_request_id_counter += 1
        re = threading.Event()
        ee = threading.Event()
        rd = {"status": 502, "statusText": "Bad Gateway", "headers": {}, "chunks": [], "error": None}
        def _h(msg):
            if msg.get("id") != rid:
                return
            mtype = msg.get("type", "")
            if mtype == "response":
                rd["status"] = msg.get("status", 200)
                rd["statusText"] = msg.get("statusText", "OK")
                rd["headers"] = msg.get("headers", {})
                re.set()
            elif mtype == "chunk":
                b64 = msg.get("data", "")
                if b64:
                    rd["chunks"].append(base64.b64decode(b64))
            elif mtype == "end":
                ee.set()
            elif mtype == "error":
                rd["error"] = msg.get("error", "Unknown error")
                re.set(); ee.set()
        utils.nm_pending_requests[rid] = _h
        utils.nm_send_msg({"type": "request_start", "id": rid, "method": method, "url": url, "headers": req_headers})
        if req_body:
            for off in range(0, len(req_body), 512*1024):
                c = req_body[off:off+512*1024]
                utils.nm_send_msg({"type": "request_chunk", "id": rid, "data": base64.b64encode(c).decode("ascii")})
        utils.nm_send_msg({"type": "request_end", "id": rid})
        return rid, re, ee, rd

    try:
        rid1, re1, ee1, rd1 = _nm_dispatch(clean_headers, body)

        if not re1.wait(timeout=120):
            raise Exception("NM response timeout (120s)")
        if rd1["error"] and len(rd1["chunks"]) == 0:
            raise Exception("NM error: %s" % rd1["error"])

        resp_headers = rd1["headers"]
        status = rd1["status"]
        stext = rd1["statusText"]

        # ---- Send HTTP head IMMEDIATELY ----
        upstream_cl = _hdr(resp_headers, "Content-Length") or ""
        drop_resp = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "content-encoding"}
        head = f"HTTP/1.1 {status} {stext}\\r\\n"
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "set-cookie" and isinstance(v, list):
                for cv in v:
                    head += f"Set-Cookie: {cv}\\r\\n"
            elif kl not in drop_resp:
                head += f"{k}: {v}\\r\\n"
        # Don't put Content-Length yet — we may need Range resume
        head += "Transfer-Encoding: chunked\\r\\n"
        head += "Connection: close\\r\\n\\r\\n"
        sock.sendall(head.encode("utf-8"))

        # ---- Stream body: send chunks as they arrive ----
        def _stream_out(rd, ee):
            """Stream chunks from rd to sock as they arrive. Returns total sent."""
            idx = 0
            deadline = time.time() + 600
            total = 0
            while not ee.is_set() or idx < len(rd["chunks"]):
                while idx < len(rd["chunks"]):
                    chunk = rd["chunks"][idx]; idx += 1
                    try:
                        hdr = f"{len(chunk):X}\\r\\n".encode()
                        sock.sendall(hdr + chunk + b"\\r\\n")
                        total += len(chunk)
                    except Exception as e2:
                        logger.debug("NM_STREAM_ERR: %s", e2)
                        return total
                if ee.is_set() and idx >= len(rd["chunks"]):
                    break
                ee.wait(0.1)
                if time.time() > deadline:
                    break
            return total

        total_sent = _stream_out(rd1, ee1)
        rd1_error = rd1["error"]

        # ---- Range resume if first request gave partial data ----
        if rd1_error and method == "GET" and not body and total_sent > 0:
            logger.debug("NM_RETRY: got %d bytes, err=%s - Range resume", total_sent, rd1_error[:80] if rd1_error else "")
            for attempt in range(3):
                rng_headers = dict(clean_headers)
                rng_headers["Range"] = "bytes=%d-" % total_sent
                rid2, re2, ee2, rd2 = _nm_dispatch(rng_headers, None)
                if not re2.wait(timeout=120):
                    logger.debug("NM_RETRY: attempt %d timeout", attempt+1)
                    utils.nm_pending_requests.pop(rid2, None)
                    break
                if rd2["error"]:
                    logger.debug("NM_RETRY: attempt %d error=%s", attempt+1, rd2["error"][:80] if rd2["error"] else "")
                sent2 = _stream_out(rd2, ee2)
                total_sent += sent2
                logger.debug("NM_RETRY: attempt %d, sent %d bytes", attempt+1, sent2)
                utils.nm_pending_requests.pop(rid2, None)
                if not rd2["error"]:
                    break
            logger.debug("NM_RESUME_DONE: total=%d bytes", total_sent)

        # ---- Chunked terminator ----
        try:
            sock.sendall(b"0\\r\\n\\r\\n")
        except Exception:
            pass

        logger.debug("NM_FINAL: status=%d body=%d err=%s", status, total_sent, rd1_error or "none")
        utils.nm_pending_requests.pop(rid1, None)

    except Exception as e:
        logger.debug("_forward_via_nm error: %s", e)
        try:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
        except Exception:
            pass
'''

data = data[:fn] + new_fn + data[fn_end:]
with open(PATH, "wb") as f:
    f.write(data)

py_compile.compile(PATH, doraise=True)
print("SUCCESS")
