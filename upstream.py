"""
Proxy Bridge — Upstream request forwarding.
Unified entry point: forward() decides NM vs urllib based on CHROME_CONNECTED.

Mitmproxy-inspired: headers sent immediately, body streamed chunk-by-chunk
to avoid buffering large responses in memory.
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
    Returns headers immediately, streams body through conn.

    Args:
        request: parsed HttpRequest with method, url, headers, body.
        conn: Connection object for writing response to client.

    Returns:
        HttpResponse with status/headers (body is empty — already streamed).
    """
    if nm.CHROME_CONNECTED:
        return _forward_via_nm(request, conn)
    else:
        return _forward_via_urllib(request, conn)


def _forward_via_nm(request: HttpRequest, conn) -> HttpResponse:
    """Forward via Chrome NM with streaming body.

    1. Send request to Chrome
    2. Wait for response headers
    3. Write headers to client immediately
    4. Stream body chunks to client as they arrive
    5. Return HttpResponse (body empty — already streamed)
    """
    with nm.nm_lock:
        req_id = nm.nm_request_counter
        nm.nm_request_counter += 1

    nm.nm_send_request(req_id, request.method, request.url,
                       request.headers, request.body)

    try:
        # Wait for response headers
        hdrs = nm.nm_wait_headers(req_id, timeout=120)

        # Check Content-Length for Range resume heuristics
        upstream_cl = _get_header(hdrs.get('headers', {}), 'Content-Length') or '0'
        expected = int(upstream_cl) if upstream_cl.isdigit() else 0

        # Write headers to client immediately (mitmproxy style)
        head = build_response_head(
            hdrs['status'], hdrs['statusText'],
            hdrs['headers'], 0, is_chunked=not upstream_cl.isdigit()
        )
        conn.sendall(head)

        # Stream body
        early = hdrs.pop('_early_chunks', [])
        total = nm.nm_stream_body(req_id, conn.sendall, timeout=600,
                                  early_chunks=early, expected=expected)

        logger.debug("NM_DONE: total=%d expected=%d", total, expected)

        return HttpResponse(
            status=hdrs['status'],
            status_text=hdrs['statusText'],
            headers=hdrs['headers'],
            body=b'',  # already streamed
        )

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

    return HttpResponse(
        status=resp.status,
        status_text=resp.reason if hasattr(resp, 'reason') else 'OK',
        headers=raw_headers,
        body=body_bytes,
    )


def _get_header(headers: dict, key: str, default: str = '') -> str:
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return default
