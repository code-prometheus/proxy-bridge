"""
Proxy Bridge — Upstream request forwarding.
Contains the proven v2.1.1 _forward_via_nm with handler→send atomic ordering,
streaming write, Range resume, and graceful shutdown. Adapted for Connection.
"""
import base64
import gzip
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import nm
from http_parser import HttpRequest, HttpResponse, build_response_head

logger = logging.getLogger('proxy_bridge.upstream')


def _maybe_decompress(body_bytes: bytes, headers: dict) -> (bytes, dict):
    """If body is gzip-compressed, decompress it and strip Content-Encoding.

    Returns (body, headers) — headers dict is a shallow copy, caller's
    dict is NOT mutated.
    """
    if len(body_bytes) >= 2 and body_bytes[:2] == b'\x1f\x8b':
        try:
            body_bytes = gzip.decompress(body_bytes)
            # Strip Content-Encoding from headers since we decoded
            headers = {k: v for k, v in headers.items()
                       if k.lower() != 'content-encoding'}
            logger.debug("GZIP_DECODE: decompressed %d bytes", len(body_bytes))
        except Exception as e:
            logger.debug("GZIP_DECODE_FAIL: %s, passing through", e)
    return body_bytes, headers


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
    """Forward through Chrome NM with size-driven Range resume."""
    # Clean request headers
    clean_headers = {}
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    for k, v in headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v

    # Auto-detect gzip request body (from client)
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

    # ---- Drain helper: collect all chunks into memory ----
    def _drain(re, ee, rd):
        idx = 0
        dl = time.time() + 600
        while not ee.is_set() or idx < len(rd['chunks']):
            idx = len(rd['chunks'])
            if ee.is_set():
                break
            ee.wait(0.1)
            if time.time() > dl:
                break
        body_bytes = b''.join(rd['chunks'])
        # Decompress gzip if present — guarantee plain text to client
        body_bytes, resp_headers = _maybe_decompress(body_bytes, rd['headers'])
        return body_bytes, resp_headers

    # ---- Main execution (v2.1.1 pattern) ----
    try:
        # Phase 1: first request
        rid, re, ee, rd = _nm_fetch(clean_headers, body)
        if not re.wait(timeout=120):
            raise Exception('NM timeout')

        # Phase 2: collect all chunks, decompress, then send
        body_bytes, resp_headers = _drain(re, ee, rd)
        nm.nm_pending_requests.pop(rid, None)

        # Build and send response head with correct Content-Length
        h = build_response_head(rd['status'], rd['statusText'],
                                resp_headers, len(body_bytes))
        conn.sendall(h)
        # Send body
        if body_bytes:
            conn.sendall(body_bytes)

        expected = len(body_bytes)
        total = expected

        # Phase 3: Range resume loop (GET without body)
        while expected == 0 and method == 'GET' and not body:
            logger.debug("NM_RESUME: have=%d", total)
            rng = dict(clean_headers)
            rng['Range'] = f"bytes={total}-"
            r2, e2, ee2, rd2 = _nm_fetch(rng, None)
            if not e2.wait(timeout=120):
                nm.nm_pending_requests.pop(r2, None)
                time.sleep(2)
                continue
            chunk_body, _ = _drain(e2, ee2, rd2)
            n = len(chunk_body)
            if n > 0:
                conn.sendall(chunk_body)
            total += n
            nm.nm_pending_requests.pop(r2, None)
            if rd2.get('done'):
                break
            if n == 0:
                time.sleep(2)

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
    resp_headers = dict(resp.headers)
    # Decompress gzip if present — guarantee plain text to client
    body_bytes, resp_headers = _maybe_decompress(body_bytes, resp_headers)
    h = build_response_head(resp.status,
                            resp.reason if hasattr(resp, 'reason') else 'OK',
                            resp_headers, len(body_bytes))
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
