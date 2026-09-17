"""
Proxy Bridge — Chrome Native Messaging protocol layer.
Handles send/recv of length-prefixed JSON messages via stdin/stdout,
and O(1) dispatch of responses to waiting callers.

Mitmproxy-inspired streaming: headers returned immediately, body streamed
via write callback — no buffering of large responses in memory.
"""
import base64
import json
import logging
import struct
import threading

logger = logging.getLogger('proxy_bridge.nm')

# ---------------------------------------------------------------------------
# NM state
# ---------------------------------------------------------------------------
nm_send_queue = None          # set by start_native_bridge()
nm_request_counter = 1
nm_lock = threading.Lock()
nm_pending_requests = {}      # {req_id: callable}
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
    Filters headers that Chrome fetch() forbids.
    """
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
# NmError
# ---------------------------------------------------------------------------

class NmError(Exception):
    """Raised when NM request fails."""
    pass


# ---------------------------------------------------------------------------
# High-level: streaming response — mitmproxy style
# ---------------------------------------------------------------------------

def nm_wait_headers(req_id: int, timeout: float = 120.0) -> dict:
    """Wait for response headers from Chrome NM.

    Returns a dict {'status': int, 'statusText': str, 'headers': dict}.
    Raises NmError on timeout or NM error.
    Caller must eventually call nm_stream_body() to drain the body.
    """
    resp_event = threading.Event()
    error_event = threading.Event()
    headers_data = {'status': 502, 'statusText': 'Bad Gateway', 'headers': {}}

    # Buffer for chunks that arrive before caller starts streaming
    early_chunks = []

    def _handler(msg: dict):
        if msg.get('id') != req_id:
            return
        mt = msg.get('type', '')
        if mt == 'response':
            headers_data['status'] = msg.get('status', 200)
            headers_data['statusText'] = msg.get('statusText', 'OK')
            headers_data['headers'] = msg.get('headers', {})
            resp_event.set()
        elif mt == 'chunk':
            b64 = msg.get('data', '')
            if b64:
                early_chunks.append(base64.b64decode(b64))
        elif mt == 'end':
            headers_data['_done'] = True
            headers_data['_early_chunks'] = early_chunks
            resp_event.set()
        elif mt == 'error':
            headers_data['_error'] = msg.get('error', 'NM error')
            error_event.set()
            resp_event.set()

    nm_pending_requests[req_id] = _handler

    # Wait for either response headers or error
    resp_event.wait(timeout=timeout)

    if error_event.is_set():
        nm_pending_requests.pop(req_id, None)
        raise NmError(headers_data.get('_error', 'NM error'))

    if not resp_event.is_set() or headers_data['status'] == 502 and not headers_data.get('_done'):
        nm_pending_requests.pop(req_id, None)
        raise NmError(f"NM timeout waiting for response headers (id={req_id})")

    # If end arrived before headers (unusual but possible — tiny response),
    # the caller will see _done=True and _early_chunks already populated.
    return headers_data


def nm_stream_body(req_id: int, write_fn, timeout: float = 600.0,
                   early_chunks: list = None, expected: int = 0) -> int:
    """Stream response body chunks to write_fn as they arrive.

    Mitmproxy-inspired: each chunk from Chrome is immediately forwarded
    to the client via write_fn. No buffering of the entire response.

    Args:
        req_id: NM request id.
        write_fn: callable(bytes) — typically Connection.sendall.
        timeout: max time to wait between chunks.
        early_chunks: chunks that arrived before caller started streaming.
        expected: Content-Length from upstream (0 if unknown). Used for
                  Range resume if streaming is interrupted.

    Returns:
        Total bytes written.
    """
    end_event = threading.Event()
    chunks_buf = list(early_chunks) if early_chunks else []  # for late chunks
    done = False
    error = None

    def _handler(msg: dict):
        nonlocal done, error
        if msg.get('id') != req_id:
            return
        mt = msg.get('type', '')
        if mt == 'chunk':
            b64 = msg.get('data', '')
            if b64:
                chunks_buf.append(base64.b64decode(b64))
        elif mt == 'end':
            done = True
            end_event.set()
        elif mt == 'error':
            error = msg.get('error', 'NM error')
            done = False
            end_event.set()

    # Replace handler to catch remaining chunks
    nm_pending_requests[req_id] = _handler

    total = 0
    try:
        # Write any early chunks first
        for c in chunks_buf:
            try:
                write_fn(c)
                total += len(c)
            except OSError:
                end_event.set()
                done = False
                return total
        chunks_buf.clear()

        # Stream remaining chunks
        while not end_event.is_set() or chunks_buf:
            # Drain buffered chunks
            while chunks_buf:
                c = chunks_buf.pop(0)
                try:
                    write_fn(c)
                    total += len(c)
                except OSError:
                    end_event.set()
                    done = False
                    return total

            if end_event.is_set():
                break

            end_event.wait(0.1)

        # Range resume for GET requests with known Content-Length
        if not done and expected > 0 and total < expected:
            logger.debug("NM_RESUME_START: have=%d expected=%d", total, expected)
            # The caller (upstream.py) handles Range resume by re-issuing the request.
            # We signal this by returning total < expected with done=False.
            # This is a clean "partial" return — caller decides what to do.

        return total

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
        logger.warning("Chrome disconnected - proxy stays alive, NM fallback to urllib")

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
