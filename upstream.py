"""
Proxy Bridge — Upstream request forwarding.
Unified entry point: forward() decides NM vs urllib based on CHROME_CONNECTED.

Key invariant: handler is registered before NM request is sent (nm_fetch_headers).
This atomic ordering prevents the race condition where Chrome's response arrives
before our handler is in nm_pending_requests.
"""
import logging
import urllib.error
import urllib.request

import nm
from http_parser import HttpRequest, HttpResponse, build_response_head

logger = logging.getLogger('proxy_bridge.upstream')


def forward(request: HttpRequest, conn) -> HttpResponse:
    """Forward an HTTP request to the upstream server.

    Uses Chrome NM if connected; falls back to urllib.
    NM path streams headers+body through conn; urllib returns complete body.
    """
    if nm.CHROME_CONNECTED:
        return _forward_via_nm(request, conn)
    else:
        return _forward_via_urllib(request, conn)


def _forward_via_nm(request: HttpRequest, conn) -> HttpResponse:
    """Forward via Chrome NM with streaming body.

    nm_fetch_headers() atomically registers the response handler THEN sends
    the request — no gap for Chrome's response to arrive before handling.
    """
    with nm.nm_lock:
        req_id = nm.nm_request_counter
        nm.nm_request_counter += 1

    try:
        # Atomic: register handler → send request → wait for headers
        hdrs = nm.nm_fetch_headers(req_id, request.method, request.url,
                                   request.headers, request.body, timeout=120)

        # Determine upstream Content-Length
        upstream_cl = _get_header(hdrs.get('headers', {}), 'Content-Length') or '0'
        expected = int(upstream_cl) if upstream_cl.isdigit() else 0

        # Write headers to client immediately (mitmproxy style)
        # If upstream has Content-Length, forward it. Otherwise chunked.
        head = build_response_head(
            hdrs['status'], hdrs['statusText'],
            hdrs['headers'], expected, is_chunked=(expected == 0)
        )
        conn.sendall(head)

        # Stream body chunks to client as they arrive
        early = hdrs.pop('_early_chunks', [])
        total = nm.nm_stream_body(req_id, conn.sendall, timeout=600,
                                  early_chunks=early, expected=expected)
        logger.debug("NM_DONE: total=%d expected=%d", total, expected)

        return HttpResponse(status=hdrs['status'], status_text=hdrs['statusText'],
                            headers=hdrs['headers'], body=b'')

    except nm.NmError as e:
        logger.debug("NM_FWD_FAIL: %s", e)
        err_body = f"Proxy error: {e}".encode('utf-8')
        return HttpResponse(status=502, status_text='Bad Gateway',
                            headers={}, body=err_body)


def _forward_via_urllib(request: HttpRequest, conn) -> HttpResponse:
    """Fallback: use urllib for direct HTTP request."""
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    clean_headers = {}
    for k, v in request.headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v

    if request.body and len(request.body) >= 2 and request.body[:2] == b'\x1f\x8b':
        has_ce = any(k.lower() == 'content-encoding' for k in request.headers)
        if not has_ce:
            clean_headers['Content-Encoding'] = 'gzip'

    data = request.body if request.body else None
    req = urllib.request.Request(request.url, data=data,
                                 headers=clean_headers, method=request.method)

    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception as e:
        logger.debug("URLLIB_FWD_ERR: %s", e)
        err_body = f"Proxy error: {e}".encode('utf-8')
        return HttpResponse(status=502, status_text='Bad Gateway',
                            headers={}, body=err_body)

    body_bytes = resp.read()
    raw_headers = dict(resp.headers)

    return HttpResponse(status=resp.status,
                        status_text=resp.reason if hasattr(resp, 'reason') else 'OK',
                        headers=raw_headers, body=body_bytes)


def _get_header(headers: dict, key: str, default: str = '') -> str:
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return default
