"""Fix _forward_via_nm: raw streaming, no chunked, with Range-based resume."""
PATH = "D:/proxy-bridge/local_proxy.py"
import py_compile

with open(PATH, "rb") as f:
    data = f.read()

# Find function boundaries using binary search
fn_start_marker = b"def _forward_via_nm(sock, method, url, headers, body):"
fn_end_marker = b"def _forward_via_urllib(sock, method, url, headers, body):"

s = data.index(fn_start_marker)
e = data.index(fn_end_marker, s)
# The blank line before _forward_via_urllib
e = data.rfind(b"\n", s, e) + 1

new_fn = b"def _forward_via_nm(sock, method, url, headers, body):\r\n"
new_fn += b'    """Forward through Chrome NM with raw streaming and Range resume."""\r\n'
new_fn += b"    clean_headers = {}\r\n"
new_fn += b'    drop_request = {"connection", "proxy-connection", "keep-alive", "host"}\r\n'
new_fn += b"    for k, v in headers.items():\r\n"
new_fn += b"        kl = k.lower()\r\n"
new_fn += b"        if kl not in drop_request:\r\n"
new_fn += b"            clean_headers[k] = v\r\n"
new_fn += b"\r\n"
new_fn += b"    if body and len(body) >= 2 and body[:2] == b'\\x1f\\x8b':\r\n"
new_fn += b"        ce_key = None\r\n"
new_fn += b"        for k in headers:\r\n"
new_fn += b"            if k.lower() == 'content-encoding':\r\n"
new_fn += b"                ce_key = k\r\n"
new_fn += b"                break\r\n"
new_fn += b"        if ce_key is None:\r\n"
new_fn += b"            clean_headers['Content-Encoding'] = 'gzip'\r\n"
new_fn += b'            logger.debug("NM_GZIP_AUTO: gzip %d-byte body", len(body))\r\n'
new_fn += b"\r\n"
new_fn += b"    # ---- Inner dispatch helper ----\r\n"
new_fn += b"    def _nm_dispatch(hdrs, bd):\r\n"
new_fn += b"        with utils.nm_lock:\r\n"
new_fn += b"            r = utils.nm_request_id_counter\r\n"
new_fn += b"            utils.nm_request_id_counter += 1\r\n"
new_fn += b"        re = threading.Event()\r\n"
new_fn += b"        ee = threading.Event()\r\n"
new_fn += b'        rd = {"status": 502, "statusText": "Bad Gateway", "headers": {}, "chunks": [], "error": None}\r\n'
new_fn += b"        def _h(msg):\r\n"
new_fn += b"            if msg.get('id') != r:\r\n"
new_fn += b"                return\r\n"
new_fn += b"            mtype = msg.get('type', '')\r\n"
new_fn += b"            if mtype == 'response':\r\n"
new_fn += b"                rd['status'] = msg.get('status', 200)\r\n"
new_fn += b"                rd['statusText'] = msg.get('statusText', 'OK')\r\n"
new_fn += b"                rd['headers'] = msg.get('headers', {})\r\n"
new_fn += b"                re.set()\r\n"
new_fn += b"            elif mtype == 'chunk':\r\n"
new_fn += b"                b64 = msg.get('data', '')\r\n"
new_fn += b"                if b64:\r\n"
new_fn += b"                    rd['chunks'].append(base64.b64decode(b64))\r\n"
new_fn += b"            elif mtype == 'end':\r\n"
new_fn += b"                ee.set()\r\n"
new_fn += b"            elif mtype == 'error':\r\n"
new_fn += b"                rd['error'] = msg.get('error', 'Unknown error')\r\n"
new_fn += b"                re.set(); ee.set()\r\n"
new_fn += b"        utils.nm_pending_requests[r] = _h\r\n"
new_fn += b'        utils.nm_send_msg({"type": "request_start", "id": r, "method": method, "url": url, "headers": hdrs})\r\n'
new_fn += b"        if bd:\r\n"
new_fn += b"            for off in range(0, len(bd), 512*1024):\r\n"
new_fn += b"                c = bd[off:off+512*1024]\r\n"
new_fn += b'                utils.nm_send_msg({"type": "request_chunk", "id": r, "data": base64.b64encode(c).decode("ascii")})\r\n'
new_fn += b'        utils.nm_send_msg({"type": "request_end", "id": r})\r\n'
new_fn += b"        return r, re, ee, rd\r\n"
new_fn += b"\r\n"
new_fn += b"    try:\r\n"
new_fn += b"        # ---- Phase 1: first NM request ----\r\n"
new_fn += b"        rid, re, ee, rd = _nm_dispatch(clean_headers, body)\r\n"
new_fn += b"        if not re.wait(timeout=120):\r\n"
new_fn += b'            raise Exception("NM response timeout (120s)")\r\n'
new_fn += b'        if rd["error"] and len(rd["chunks"]) == 0:\r\n'
new_fn += b'            raise Exception("NM error: %s" % rd["error"])\r\n'
new_fn += b"\r\n"
new_fn += b"        # ---- Phase 2: send HTTP head immediately, then stream raw bytes ----\r\n"
new_fn += b'        resp_headers = rd["headers"]\r\n'
new_fn += b'        status = rd["status"]\r\n'
new_fn += b'        stext = rd["statusText"]\r\n'
new_fn += b'        drop_resp = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "content-encoding"}\r\n'
new_fn += b"        head = f\"HTTP/1.1 {status} {stext}\\\\r\\\\n\"\r\n"
new_fn += b"        for k, v in resp_headers.items():\r\n"
new_fn += b"            kl = k.lower()\r\n"
new_fn += b'            if kl == "set-cookie" and isinstance(v, list):\r\n'
new_fn += b"                for cv in v:\r\n"
new_fn += b'                    head += f"Set-Cookie: {cv}\\\\r\\\\n"\r\n'
new_fn += b"            elif kl not in drop_resp:\r\n"
new_fn += b'                head += f"{k}: {v}\\\\r\\\\n"\r\n'
new_fn += b'        head += "Connection: close\\\\r\\\\n\\\\r\\\\n"\r\n'
new_fn += b'        sock.sendall(head.encode("utf-8"))\r\n'
new_fn += b"\r\n"
new_fn += b"        # Stream raw bytes as NM delivers them, no framing\r\n"
new_fn += b"        idx = 0\r\n"
new_fn += b"        deadline = time.time() + 600\r\n"
new_fn += b'        while not ee.is_set() or idx < len(rd["chunks"]):\r\n'
new_fn += b'            while idx < len(rd["chunks"]):\r\n'
new_fn += b"                try:\r\n"
new_fn += b'                    sock.sendall(rd["chunks"][idx])\r\n'
new_fn += b"                except Exception:\r\n"
new_fn += b"                    ee.set()\r\n"
new_fn += b"                    break\r\n"
new_fn += b"                idx += 1\r\n"
new_fn += b'            if ee.is_set() and idx >= len(rd["chunks"]):\r\n'
new_fn += b"                break\r\n"
new_fn += b"            ee.wait(0.1)\r\n"
new_fn += b"            if time.time() > deadline:\r\n"
new_fn += b"                break\r\n"
new_fn += b'        total_sent = sum(len(c) for c in rd["chunks"][:idx])\r\n'
new_fn += b'        utils.nm_pending_requests.pop(rid, None)\r\n'
new_fn += b"\r\n"
new_fn += b"        # ---- Phase 3: Range resume if NM gave partial data and error ----\r\n"
new_fn += b'        if rd["error"] and method == "GET" and not body and total_sent > 0:\r\n'
new_fn += b'            logger.debug("NM_RETRY: got %d bytes, err=%s. Trying Range resume.", total_sent, rd["error"][:80])'
new_fn += b"\r\n"
new_fn += b"            for attempt in range(3):\r\n"
new_fn += b"                rng_hdrs = dict(clean_headers)\r\n"
new_fn += b'                rng_hdrs["Range"] = "bytes=%d-" % total_sent\r\n'
new_fn += b"                rid2, re2, ee2, rd2 = _nm_dispatch(rng_hdrs, None)\r\n"
new_fn += b"                if not re2.wait(timeout=120):\r\n"
new_fn += b'                    logger.debug("NM_RETRY: attempt %d timeout", attempt+1)\r\n'
new_fn += b"                    utils.nm_pending_requests.pop(rid2, None)\r\n"
new_fn += b"                    break\r\n"
new_fn += b'                idx2 = 0\r\n'
new_fn += b"                deadline2 = time.time() + 600\r\n"
new_fn += b'                while not ee2.is_set() or idx2 < len(rd2["chunks"]):\r\n'
new_fn += b'                    while idx2 < len(rd2["chunks"]):\r\n'
new_fn += b"                        try:\r\n"
new_fn += b'                            sock.sendall(rd2["chunks"][idx2])\r\n'
new_fn += b"                        except Exception:\r\n"
new_fn += b"                            ee2.set()\r\n"
new_fn += b"                            break\r\n"
new_fn += b"                        idx2 += 1\r\n"
new_fn += b'                    if ee2.is_set() and idx2 >= len(rd2["chunks"]):\r\n'
new_fn += b"                        break\r\n"
new_fn += b"                    ee2.wait(0.1)\r\n"
new_fn += b"                    if time.time() > deadline2:\r\n"
new_fn += b"                        break\r\n"
new_fn += b'                sent2 = sum(len(c) for c in rd2["chunks"][:idx2])\r\n'
new_fn += b"                total_sent += sent2\r\n"
new_fn += b'                logger.debug("NM_RETRY: attempt %d sent %d bytes, err=%s", attempt+1, sent2, rd2.get("error", "none")[:80])'
new_fn += b"\r\n"
new_fn += b"                utils.nm_pending_requests.pop(rid2, None)\r\n"
new_fn += b'                if not rd2["error"]:\r\n'
new_fn += b"                    break\r\n"
new_fn += b'            logger.debug("NM_RESUME_DONE: total=%d bytes", total_sent)\r\n'
new_fn += b"\r\n"
new_fn += b'        logger.debug("NM_FINAL: sent=%d chunks, total_body=%d bytes, err=%s", idx, total_sent if rd["error"] else total_sent, rd.get("error", "none") or "none")\r\n'
new_fn += b"\r\n"
new_fn += b"    except Exception as e:\r\n"
new_fn += b'        logger.debug("_forward_via_nm error: %s", e)\r\n'
new_fn += b"        try:\r\n"
new_fn += b'            sock.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")\r\n'
new_fn += b"        except Exception:\r\n"
new_fn += b"            pass\r\n"

data = data[:s] + new_fn + data[e:]
with open(PATH, "wb") as f:
    f.write(data)

py_compile.compile(PATH, doraise=True)
print("SUCCESS")
