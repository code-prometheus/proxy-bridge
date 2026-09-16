"""
Proxy Bridge — Chrome Native Messaging protocol layer.
Handles send/recv of length-prefixed JSON messages via stdin/stdout,
and O(1) dispatch of responses to waiting callers.
"""
import base64
import json
import logging
import struct
import threading
import time

logger = logging.getLogger('proxy_bridge.nm')

# ---------------------------------------------------------------------------
# NM state (shared with utils.py for now — imported by proxy.py)
# ---------------------------------------------------------------------------
nm_send_queue = None        # set by start_native_bridge()
nm_request_counter = 1
nm_lock = threading.Lock()
nm_pending_requests = {}     # {req_id: callable}
CHROME_CONNECTED = False


# ---------------------------------------------------------------------------
# Sending to Chrome
# ---------------------------------------------------------------------------

def _nm_send(msg_dict: dict) -> None:
    """Enqueue a JSON-serialisable dict for delivery to Chrome via stdout."""
    if nm_send_queue is not None:
        nm_send_queue.put(msg_dict)


def nm_send_request(req_id: int, method: str, url: str,
                    headers: dict, body: bytes = None) -> None:
    """Send a complete HTTP request to Chrome via NM.

    Splits body into 512KB base64 chunks (Chrome NM 1MB message limit).
    """
    # Filter headers that Chrome fetch() forbids
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    clean_headers = {}
    for k, v in headers.items():
        if k.lower() not in drop:
            clean_headers[k] = v

    # Auto-detect gzip body
    if body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
        has_ce = any(k.lower() == 'content-encoding' for k in headers)
        if not has_ce:
            clean_headers['Content-Encoding'] = 'gzip'
            logger.debug("NM_GZIP_AUTO: added Content-Encoding: gzip for %d-byte body", len(body))

    _nm_send({
        'type': 'request_start',
        'id': req_id,
        'method': method,
        'url': url,
        'headers': clean_headers,
    })

    if body:
        CHUNK = 512 * 1024
        for off in range(0, len(body), CHUNK):
            chunk = body[off:off + CHUNK]
            _nm_send({
                'type': 'request_chunk',
                'id': req_id,
                'data': base64.b64encode(chunk).decode('ascii'),
            })

    _nm_send({'type': 'request_end', 'id': req_id})


# ---------------------------------------------------------------------------
# Receiving from Chrome (O(1) dispatch)
# ---------------------------------------------------------------------------

def nm_dispatch(msg: dict) -> None:
    """Route an NM message to the registered handler by id. O(1)."""
    # Ignore ping
    if msg.get('type') == 'ping':
        return

    req_id = msg.get('id')
    if req_id is None:
        return

    handler = nm_pending_requests.get(req_id)
    if handler is None:
        logger.debug("NM_DISPATCH: no handler for id=%d", req_id)
        return

    try:
        handler(msg)
    except Exception as e:
        logger.debug("NM handler error for id=%d: %s", req_id, e)


# ---------------------------------------------------------------------------
# High-level: wait for a complete response
# ---------------------------------------------------------------------------

class NmError(Exception):
    """Raised when NM request fails."""
    pass


def nm_wait_response(req_id: int, timeout: float = 120.0
                     ) -> 'HttpResponse':
    """Wait for a complete response from Chrome NM for the given request id.

    Returns a complete HttpResponse (body fully assembled).
    Raises NmError on timeout or NM error.

    Handles size-driven Range resume transparently for GET requests.
    """
    from http_parser import HttpResponse  # deferred import to avoid circular

    resp_event = threading.Event()
    end_event = threading.Event()
    response_data = {
        'status': 502,
        'statusText': 'Bad Gateway',
        'headers': {},
        'chunks': [],
        'done': False,
    }

    def _handler(msg: dict):
        if msg.get('id') != req_id:
            return
        mt = msg.get('type', '')
        if mt == 'response':
            response_data['status'] = msg.get('status', 200)
            response_data['statusText'] = msg.get('statusText', 'OK')
            response_data['headers'] = msg.get('headers', {})
            resp_event.set()
        elif mt == 'chunk':
            b64 = msg.get('data', '')
            if b64:
                response_data['chunks'].append(base64.b64decode(b64))
        elif mt == 'end':
            response_data['done'] = True
            end_event.set()
        elif mt == 'error':
            response_data['done'] = False
            resp_event.set()
            end_event.set()

    # Register handler
    nm_pending_requests[req_id] = _handler

    try:
        # Wait for response headers
        if not resp_event.wait(timeout=timeout):
            raise NmError(f"NM timeout waiting for response headers (id={req_id})")

        # If error, fail immediately
        if not response_data['done'] and end_event.is_set():
            raise NmError(f"NM error response (id={req_id})")

        # Wait for body to complete
        if not end_event.wait(timeout=timeout):
            raise NmError(f"NM timeout waiting for body (id={req_id})")

        if not response_data.get('done'):
            raise NmError(f"NM incomplete response (id={req_id})")

        # Assemble body
        body = b''.join(response_data['chunks'])

        return HttpResponse(
            status=response_data['status'],
            status_text=response_data['statusText'],
            headers=response_data['headers'],
            body=body,
        )

    finally:
        nm_pending_requests.pop(req_id, None)


# ---------------------------------------------------------------------------
# I/O threads
# ---------------------------------------------------------------------------

def native_writer_thread():
    """Drain nm_send_queue, write length-prefixed JSON to stdout."""
    import sys
    while True:
        try:
            msg = nm_send_queue.get()
            if msg is None:
                break
            json_data = json.dumps(msg, ensure_ascii=False)
            json_bytes = json_data.encode('utf-8')
            length_bytes = struct.pack('<I', len(json_bytes))
            sys.stdout.buffer.write(length_bytes + json_bytes)
            sys.stdout.buffer.flush()
        except Exception as e:
            logger.debug("native_writer_thread error: %s", e)
            break


def native_reader_thread():
    """Read length-prefixed JSON from stdin, dispatch via nm_dispatch()."""
    import sys
    global CHROME_CONNECTED

    CHROME_CONNECTED = True
    logger.info("Chrome extension connected via Native Messaging")

    try:
        while True:
            raw_length = sys.stdin.buffer.read(4)
            if not raw_length or len(raw_length) < 4:
                logger.warning("NM stdin EOF — Chrome disconnected")
                break
            msg_length = struct.unpack('<I', raw_length)[0]
            if msg_length > 16 * 1024 * 1024:
                logger.warning("NM message too large: %d bytes, skipping", msg_length)
                continue
            json_bytes = sys.stdin.buffer.read(msg_length)
            if not json_bytes or len(json_bytes) < msg_length:
                break
            msg = json.loads(json_bytes.decode('utf-8', errors='replace'))
            nm_dispatch(msg)
    except Exception as e:
        logger.debug("native_reader_thread error: %s", e)
    finally:
        CHROME_CONNECTED = False
        logger.warning("Chrome disconnected - proxy stays alive, pending urllib fallback")

        # Fail all pending NM requests
        for rid in list(nm_pending_requests.keys()):
            try:
                nm_pending_requests[rid]({'type': 'error', 'id': rid, 'error': 'NM disconnected'})
            except Exception:
                pass
        nm_pending_requests.clear()


def start_native_bridge(send_queue):
    """Launch reader and writer threads for Chrome Native Messaging.

    Args:
        send_queue: queue.Queue() used to send messages to Chrome.

    Returns:
        (writer_thread, reader_thread)
    """
    global nm_send_queue
    nm_send_queue = send_queue

    writer = threading.Thread(target=native_writer_thread, daemon=True, name='nm-writer')
    reader = threading.Thread(target=native_reader_thread, daemon=True, name='nm-reader')
    writer.start()
    reader.start()
    return writer, reader
