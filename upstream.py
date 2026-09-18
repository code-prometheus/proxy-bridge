"""
Proxy Bridge — Upstream request forwarding.
Contains the proven v2.1.1 _forward_via_nm with handler→send atomic ordering,
streaming write, Range resume, and graceful shutdown. Adapted for Connection.
"""
import base64
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import nm
from http_parser import HttpRequest, HttpResponse, build_response_head

logger = logging.getLogger('proxy_bridge.upstream')


def forward(request: HttpRequest, conn) -> HttpResponse:
    """Forward an HTTP request to the upstream server."""
    if nm.CHROME_CONNECTED:
        return _forward_via_nm(conn, request.method, request.url,
                               request.headers, request.body)
    else:
        return _forward_via_urllib(conn, request.method, request.url,
                                   request.headers, request.body)


# ===========================================================================
# NM forwarding — v2.1.1 proven pattern
# ===========================================================================

def _forward_via_nm(conn, method, url, headers, body):
    """Forward through Chrome NM with size-driven Range resume.

    This is the exact v2.1.1 _forward_via_nm logic:
    1. _nm_fetch: register handler → send request → atomic
    2. _stream: drain chunks as they arrive → write to client
    3. Range resume: if body incomplete, re-fetch with Range header

    Adapted: sock.sendall → conn.sendall. conn.shutdown removed (TLS incompatibility).
    """
    # Clean request headers
    clean_headers = {}
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    for k, v in headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v

    # Auto-detect gzip body
    if body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
        for k in headers:
            if k.lower() == 'content-encoding':
                clean_headers['Content-Encoding'] = 'gzip'
                break

    # ---- NM fetch helper (v2.1.1 pattern) ----
    def _nm_fetch(hdrs, bd):
        with nm.nm_lock:
            r = nm.nm_request_counter
            nm.nm_request_counter += 1
        re = threading.Event()
        ee = threading.Event()
        rd = {'status': 502, 'statusText': 'Bad Gateway',
              'headers': {}, 'chunks': [], 'done': False}

        def _h(msg):
            if msg.get('id') != r:
                return
            mt = msg.get('type', '')
            if mt == 'response':
                rd['status'] = msg.get('status', 200)
                rd['statusText'] = msg.get('statusText', 'OK')
                rd['headers'] = msg.get('headers', {})
                re.set()
            elif mt == 'chunk':
                b64 = msg.get('data', '')
                if b64:
                    rd['chunks'].append(base64.b64decode(b64))
            elif mt == 'end':
                rd['done'] = True
                ee.set()
            elif mt == 'error':
                rd['done'] = False
                re.set()
                ee.set()

        # CRITICAL: register handler BEFORE sending (v2.1.1 ordering)
        nm.nm_pending_requests[r] = _h
        nm.nm_send_request(r, method, url, hdrs, bd)
        return r, re, ee, rd

    # ---- Stream helper (v2.1.1 pattern) ----
    def _stream(re, ee, rd):
        idx = 0
        dl = time.time() + 600
        sock_err = False
        while not ee.is_set() or idx < len(rd['chunks']):
            while idx < len(rd['chunks']):
                try:
                    conn.sendall(rd['chunks'][idx])
                except OSError:
                    ee.set()
                    sock_err = True
                    break
                idx += 1
            if ee.is_set() and idx >= len(rd['chunks']):
                break
            ee.wait(0.1)
            if time.time() > dl:
                break
        return sum(len(c) for c in rd['chunks'][:idx]), sock_err

    # ---- Main execution (v2.1.1 pattern) ----
    try:
        # Phase 1: first request
        rid, re, ee, rd = _nm_fetch(clean_headers, body)
        if not re.wait(timeout=120):
            raise Exception('NM timeout')

        # Get expected size from upstream Content-Length
        upstream_cl = _hdr(rd['headers'], 'Content-Length') or '0'
        expected = int(upstream_cl) if upstream_cl.isdigit() else 0

        # Phase 2: send response head to client immediately
        drop_r = {'connection', 'proxy-connection', 'keep-alive',
                  'transfer-encoding', 'content-encoding'}
        h = f"HTTP/1.1 {rd['status']} {rd['statusText']}\r\n"
        for k, v in rd['headers'].items():
            kl = k.lower()
            if kl == 'set-cookie' and isinstance(v, list):
                for cv in v:
                    h += f"Set-Cookie: {cv}\r\n"
            elif kl not in drop_r:
                h += f"{k}: {v}\r\n"
        h += 'Connection: close\r\n\r\n'
        conn.sendall(h.encode('utf-8'))

        # Phase 3: stream body chunks + Range resume loop
        total, dead = _stream(re, ee, rd)
        nm.nm_pending_requests.pop(rid, None)

        if not expected:
            if rd.get('done') and method == 'GET' and not body:
                expected = total
            elif method == 'GET' and not body:
                expected = 10 * 1024 * 1024 * 1024  # 10GB cap

        while total < expected and method == 'GET' and not body and not dead:
            logger.debug("NM_RESUME: have=%d need=%d", total, expected)
            rng = dict(clean_headers)
            rng['Range'] = f"bytes={total}-"
            r2, e2, ee2, rd2 = _nm_fetch(rng, None)
            if not e2.wait(timeout=120):
                nm.nm_pending_requests.pop(r2, None)
                time.sleep(2)
                continue
            cl2 = _hdr(rd2['headers'], 'Content-Length') or ''
            cl2n = int(cl2) if cl2.isdigit() else 0
            if cl2n > 0:
                expected = total + cl2n
            n, dead = _stream(e2, ee2, rd2)
            total += n
            nm.nm_pending_requests.pop(r2, None)
            if rd2.get('done') and total >= expected:
                break
            if n == 0:
                time.sleep(2)
            if dead:
                logger.debug("NM_CLIENT_DEAD: client disconnected, stopping resume")
        logger.debug("NM_DONE: total=%d", total)

    except Exception as e:
        logger.debug("_forward_via_nm err: %s", e)
        try:
            conn.sendall(
                b'HTTP/1.1 502 Bad Gateway\r\n'
                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
        except OSError:
            pass

    return HttpResponse(status=502, status_text='OK', headers={}, body=b'')


# ===========================================================================
# urllib fallback
# ===========================================================================

def _forward_via_urllib(conn, method, url, headers, body):
    """Fallback: use urllib for direct HTTP request."""
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    clean_headers = {}
    for k, v in headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v
    if body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
        if not any(k.lower() == 'content-encoding' for k in headers):
            clean_headers['Content-Encoding'] = 'gzip'
    data = body if body else None
    req = urllib.request.Request(url, data=data,
                                 headers=clean_headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception as e:
        logger.debug("URLLIB_FWD_ERR: %s", e)
        err_body = f"Proxy error: {e}".encode('utf-8')
        h = build_response_head(502, 'Bad Gateway', {}, len(err_body))
        try:
            conn.sendall(h + err_body)
        except OSError:
            pass
        return HttpResponse(status=502, status_text='Bad Gateway',
                            headers={}, body=b'')

    body_bytes = resp.read()
    h = build_response_head(resp.status,
                            resp.reason if hasattr(resp, 'reason') else 'OK',
                            dict(resp.headers), len(body_bytes))
    try:
        conn.sendall(h + body_bytes)
    except OSError as e:
        logger.debug("_forward_via_urllib send error: %s", e)

    return HttpResponse(status=resp.status,
                        status_text='OK', headers={}, body=b'')


def _hdr(headers, key):
    """Case-insensitive header lookup."""
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return ''
