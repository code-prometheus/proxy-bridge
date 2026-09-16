"""
Proxy Bridge — Upstream request forwarding.
Unified entry point: forward() decides NM vs urllib based on CHROME_CONNECTED.
"""
import logging
import urllib.error
import urllib.request

import nm
from http_parser import HttpRequest, HttpResponse

logger = logging.getLogger('proxy_bridge.upstream')


def forward(request: HttpRequest, conn) -> HttpResponse:
    """Forward an HTTP request to the upstream server.

    Uses Chrome NM if connected, otherwise falls back to urllib.

    Args:
        request: parsed HttpRequest with method, url, headers, body.
        conn: Connection object (needed for urllib fallback to send response).

    Returns:
        Complete HttpResponse ready to write back to client.
    """
    if nm.CHROME_CONNECTED:
        return _forward_via_nm(request)
    else:
        return _forward_via_urllib(request, conn)


def _forward_via_nm(request: HttpRequest) -> HttpResponse:
    """Forward via Chrome Native Messaging.

    Sends request → waits for complete response → returns HttpResponse.
    Simple and clean — no streaming, no Range resume in this layer.
    """
    with nm.nm_lock:
        req_id = nm.nm_request_counter
        nm.nm_request_counter += 1

    nm.nm_send_request(req_id, request.method, request.url,
                       request.headers, request.body)

    try:
        return nm.nm_wait_response(req_id, timeout=120)
    except nm.NmError as e:
        logger.debug("NM_FWD_FAIL: %s", e)
        return HttpResponse(
            status=502,
            status_text='Bad Gateway',
            headers={},
            body=f"Proxy error: {e}".encode('utf-8'),
        )


def _forward_via_urllib(request: HttpRequest, conn) -> HttpResponse:
    """Fallback: use urllib for direct HTTP request.

    Args:
        request: parsed HttpRequest.
        conn: Connection (unused for direct urllib, kept for interface uniformity).

    Returns:
        HttpResponse.
    """
    # Filter headers
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    clean_headers = {}
    for k, v in request.headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v

    # Auto-detect gzip body
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
        return HttpResponse(
            status=502,
            status_text='Bad Gateway',
            headers={},
            body=f"Proxy error: {e}".encode('utf-8'),
        )

    body_bytes = resp.read()
    raw_headers = dict(resp.headers)

    return HttpResponse(
        status=resp.status,
        status_text=resp.reason if hasattr(resp, 'reason') else 'OK',
        headers=raw_headers,
        body=body_bytes,
    )
