"""Patch _forward_via_nm with Range-based resume. ALL NM, never urllib."""
PATH = "D:/proxy-bridge/local_proxy.py"
import py_compile

with open(PATH, "rb") as f:
    data = f.read()

fn = data.index(b"def _forward_via_nm(")
fn_end = data.index(b"\r\ndef _forward_via_urllib(", fn) + 2

new_fn = b'''def _forward_via_nm(sock, method, url, headers, body):
    """Forward request through Chrome NM with Range-based resume on failure."""
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
            logger.debug("NM_GZIP_AUTO: added Content-Encoding: gzip for %d-byte body", len(body))

    # ---- Inner NM fetch helper (always Chrome fetch(), never urllib) ----
    def _nm_fetch(req_headers, req_body):
        """Send one NM request. Returns (status, stext, headers_dict, body_bytes, error_or_None)."""
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
        try:
            utils.nm_send_msg({"type": "request_start", "id": rid, "method": method, "url": url, "headers": req_headers})
            if req_body:
                for off in range(0, len(req_body), 512*1024):
                    c = req_body[off:off+512*1024]
                    utils.nm_send_msg({"type": "request_chunk", "id": rid, "data": base64.b64encode(c).decode("ascii")})
            utils.nm_send_msg({"type": "request_end", "id": rid})

            if not re.wait(timeout=120):
                return (502, "Gateway Timeout", {}, b"", "NM response timeout")
            if rd["error"]:
                body_bytes = b"".join(rd["chunks"])
                return (rd["status"], rd["statusText"], rd["headers"], body_bytes, rd["error"])

            if not ee.wait(timeout=600):
                body_bytes = b"".join(rd["chunks"])
                return (rd["status"], rd["statusText"], rd["headers"], body_bytes, "Partial: %d bytes, NM body timeout" % len(body_bytes))

            body_bytes = b"".join(rd["chunks"])
            return (rd["status"], rd["statusText"], rd["headers"], body_bytes, None)
        finally:
            utils.nm_pending_requests.pop(rid, None)

    # ---- Main flow: try normal, retry with Range on partial failure ----
    try:
        status, stext, resp_headers, body_bytes, error = _nm_fetch(clean_headers, body)

        if error and method == "GET" and not body and len(body_bytes) > 0:
            total = len(body_bytes)
            logger.debug("NM_RETRY: got %d bytes, error=%s - retrying with Range", total, error[:80] if error else "")
            for attempt in range(3):
                rng_headers = dict(clean_headers)
                rng_headers["Range"] = "bytes=%d-" % total
                _, _, _, b2, e2 = _nm_fetch(rng_headers, None)
                logger.debug("NM_RETRY: attempt %d got %d bytes, err=%s", attempt+1, len(b2), (e2 or "none")[:80])
                body_bytes += b2
                if not e2:
                    error = None
                    break
            logger.debug("NM_RESUME_DONE: total=%d bytes, final_err=%s", len(body_bytes), error or "none")

        if error and len(body_bytes) == 0:
            raise Exception(error)

        # ---- Send response to client ----
        drop_resp = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "content-encoding"}
        head = f"HTTP/1.1 {status} {stext}\\r\\n"
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "set-cookie" and isinstance(v, list):
                for cv in v:
                    head += f"Set-Cookie: {cv}\\r\\n"
            elif kl not in drop_resp:
                head += f"{k}: {v}\\r\\n"
        head += f"Content-Length: {len(body_bytes)}\\r\\n"
        head += "Connection: close\\r\\n\\r\\n"
        sock.sendall(head.encode("utf-8"))
        for i in range(0, len(body_bytes), 4*1024*1024):
            sock.sendall(body_bytes[i:i+4*1024*1024])
        logger.debug("NM_FINAL: status=%d body=%d err=%s", status, len(body_bytes), error or "none")

    except Exception as e:
        logger.debug("_forward_via_nm error: %s", e)
        try:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
        except Exception:
            pass
    finally:
        pass
'''

data = data[:fn] + new_fn + data[fn_end:]
with open(PATH, "wb") as f:
    f.write(data)

py_compile.compile(PATH, doraise=True)
print("SUCCESS")
