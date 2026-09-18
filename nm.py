"""
Proxy Bridge — Chrome Native Messaging transport layer.
Length-prefixed JSON via stdin/stdout. O(1) dispatch.
No high-level request/response logic here — that's in upstream.py.
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
nm_send_queue = None
nm_request_counter = 1
nm_lock = threading.Lock()
nm_pending_requests = {}  # {req_id: callable}
CHROME_CONNECTED = False
shutdown_event = None


# ---------------------------------------------------------------------------
# Sending to Chrome
# ---------------------------------------------------------------------------

def nm_send_msg(msg_dict: dict) -> None:
    """Enqueue a JSON-serialisable dict for delivery to Chrome via stdout."""
    if nm_send_queue is not None:
        nm_send_queue.put(msg_dict)


def nm_send_request(req_id: int, method: str, url: str,
                    headers: dict, body: bytes = None) -> None:
    """Send a complete HTTP request to Chrome via NM (request_start + chunks + end)."""
    drop = {'connection', 'proxy-connection', 'keep-alive', 'host'}
    clean = {}
    for k, v in headers.items():
        if k.lower() not in drop:
            clean[k] = v
    if body and len(body) >= 2 and body[:2] == b'\x1f\x8b':
        if not any(k.lower() == 'content-encoding' for k in headers):
            clean['Content-Encoding'] = 'gzip'
    nm_send_msg({'type': 'request_start', 'id': req_id,
                 'method': method, 'url': url, 'headers': clean})
    if body:
        for off in range(0, len(body), 512 * 1024):
            nm_send_msg({'type': 'request_chunk', 'id': req_id,
                         'data': base64.b64encode(body[off:off + 512 * 1024]).decode('ascii')})
    nm_send_msg({'type': 'request_end', 'id': req_id})


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
# I/O threads
# ---------------------------------------------------------------------------

def native_writer_thread():
    """Drain nm_send_queue, write length-prefixed JSON to original stdout."""
    import utils as _utils
    while True:
        try:
            msg = nm_send_queue.get()
            if msg is None:
                break
            json_data = json.dumps(msg, ensure_ascii=False)
            json_bytes = json_data.encode('utf-8')
            length_bytes = struct.pack('<I', len(json_bytes))
            _utils.original_stdout_buffer.write(length_bytes + json_bytes)
            _utils.original_stdout_buffer.flush()
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
        logger.warning("Chrome disconnected - proxy stays alive (urllib fallback)")
        for rid in list(nm_pending_requests.keys()):
            try:
                nm_pending_requests[rid]({'type': 'error', 'id': rid, 'error': 'NM disconnected'})
            except Exception:
                pass
        nm_pending_requests.clear()
        # Note: do NOT set shutdown_event — proxy stays alive for urllib fallback.
        # When Chrome reconnects, it will spawn a new proxy process (SO_REUSEADDR).
        # shutdown_event is only relevant for graceful process exit, which we
        # don't need here.


def start_native_bridge(send_queue, shutdown_evt):
    """Launch reader and writer threads for Chrome Native Messaging."""
    global nm_send_queue, shutdown_event
    nm_send_queue = send_queue
    shutdown_event = shutdown_evt
    writer = threading.Thread(target=native_writer_thread, daemon=True, name='nm-writer')
    reader = threading.Thread(target=native_reader_thread, daemon=True, name='nm-reader')
    writer.start()
    reader.start()
    return writer, reader
