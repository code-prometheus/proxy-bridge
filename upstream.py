"""
Proxy Bridge — Upstream request forwarding.
Queue-based streaming: chunks flow NM→proxy→client without buffering.
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

    Chrome fetch() transparently decompresses all standard encodings
    (gzip, deflate, brotli), so the body we receive is ALWAYS plain text.
    Any Content-Encoding header from the upstream is stale — strip it
    unconditionally to avoid poisoning clients.

    Returns (body, headers) — headers dict is a shallow copy, caller's
    dict is NOT mutated.
    """
    if len(body_bytes) >= 2 and body_bytes[:2] == b'\x1f\x8b':
        try:
            body_bytes = gzip.decompress(body_bytes)
            logger.debug("GZIP_DECODE: decompressed %d bytes", len(body_bytes))
        except Exception as e:
            logger.debug("GZIP_DECODE_FAIL: %s, passing through", e)
    # ALWAYS strip Content-Encoding — Chrome fetch already decompressed.
    # If we leave it, clients try to decompress plain text and fail.
    encoding = headers.get('Content-Encoding', headers.get('content-encoding', ''))
    if encoding:
        headers = {k: v for k, v in headers.items()
                   if k.lower() != 'content-encoding'}
        logger.debug("CE_STRIP: removed stale Content-Encoding=%s", encoding)
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
# NM forwarding — queue-based streaming (no body buffering)
# ===========================================================================

def _forward_via_nm(conn, method, url, headers, body):
    """Forward through Chrome NM — streams response body chunk by chunk.

    Uses queue-based streaming: NM handler pushes chunks to a queue,
    main thread reads and sends them immediately via chunked TE.
    This avoids buffering the entire response in memory (fixes 184MB+
    truncation) and eliminates the gzip Content-Encoding poison.
    """
    import queue as _qmod

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

    try:
        with nm.nm_lock:
            rid = nm.nm_request_counter
            nm.nm_request_counter += 1

        chunk_queue = _qmod.Queue()
        header_evt = threading.Event()

        def _h(msg):
            if msg.get('id') != rid:
                return
            mt = msg.get('type', '')
            if mt == 'response':
                chunk_queue.put(('head', msg))
                header_evt.set()
            elif mt == 'chunk':
                b64 = msg.get('data', '')
                if b64:
                    chunk_queue.put(('data', base64.b64decode(b64)))
            elif mt == 'end':
                chunk_queue.put(('end', None))
            elif mt == 'error':
                chunk_queue.put(('error', msg.get('error', 'unknown')))

        nm.nm_pending_requests[rid] = _h
        nm.nm_send_request(rid, method, url, clean_headers, body)

        # Wait for response header
        if not header_evt.wait(timeout=120):
            raise Exception('NM timeout waiting for response header')

        htyp, hdr_msg = chunk_queue.get(timeout=5)
        if htyp != 'head':
            raise Exception(f'Expected header, got {htyp}')

        # Build clean response headers — strip Content-Encoding (Chrome decompressed)
        resp_hdrs = {}
        for k, v in hdr_msg.get('headers', {}).items():
            kl = k.lower()
            if kl == 'set-cookie' and isinstance(v, list):
                resp_hdrs[k] = v
            elif kl != 'content-encoding':
                resp_hdrs[k] = v

        status = hdr_msg.get('status', 200)
        status_text = hdr_msg.get('statusText', 'OK')

        # Send response head (chunked TE — we stream, don't know final size)
        head = build_response_head(status, status_text, resp_hdrs, 0, is_chunked=True)
        conn.sendall(head)

        # Stream body chunks
        total = 0
        deadline = time.time() + 600
        while True:
            try:
                typ, val = chunk_queue.get(timeout=min(30, deadline - time.time()))
            except _qmod.Empty:
                if time.time() >= deadline:
                    break
                continue

            if typ == 'data':
                # Chunked encoding: hex-size + CRLF + data + CRLF
                conn.sendall(f"{len(val):X}\r\n".encode('ascii') + val + b'\r\n')
                total += len(val)
            elif typ == 'end':
                break
            elif typ == 'error':
                logger.debug("NM_STREAM_ERR: %s", val)
                break

        # Chunked terminator
        conn.sendall(b'0\r\n\r\n')
        nm.nm_pending_requests.pop(rid, None)
        logger.debug("NM_STREAM_DONE: total=%d", total)

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
