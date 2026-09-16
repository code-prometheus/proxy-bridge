"""
Proxy Bridge — HTTP message parsing and serialization.
Pure functions for parsing HTTP/1.1 headers and reading bodies.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger('proxy_bridge.http')


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass
class HttpRequest:
    method: str           # GET, POST, CONNECT, ...
    url: str              # /path or https://host/path
    headers: dict         # {key: value}  — case preserved as received
    body: bytes = b''     # complete body (already read from socket)

    def get_header(self, key: str, default: str = '') -> str:
        """Case-insensitive header lookup."""
        kl = key.lower()
        for k, v in self.headers.items():
            if k.lower() == kl:
                return v
        return default


@dataclass
class HttpResponse:
    status: int           # 200, 502, ...
    status_text: str      # OK, Bad Gateway, ...
    headers: dict         # {key: value}
    body: bytes = b''     # complete body

    def get_header(self, key: str, default: str = '') -> str:
        kl = key.lower()
        for k, v in self.headers.items():
            if k.lower() == kl:
                return v
        return default


# ---------------------------------------------------------------------------
# Header parse (pure function — no I/O)
# ---------------------------------------------------------------------------

def parse_http_header(data: bytes):
    """Parse HTTP/1.1 request line and headers from raw bytes.

    Args:
        data: raw bytes starting with request line, ending at least at \\r\\n\\r\\n.

    Returns:
        (method, url, headers_dict, body_prefix_bytes)
        On parse failure returns (None, None, None, None).
        On oversized header returns ('TOO_LARGE', data[:65536], None, None).
    """
    if len(data) > 65536:
        logger.warning("HEADER_TOO_LARGE: len=%d", len(data))
        return 'TOO_LARGE', data[:65536], None, None

    header_end = data.find(b'\r\n\r\n')
    if header_end >= 0:
        header_bytes = data[:header_end]
        body_prefix = data[header_end + 4:]
        sep = '\r\n'
    else:
        header_end = data.find(b'\n\n')
        if header_end < 0:
            return None, None, None, None
        header_bytes = data[:header_end]
        body_prefix = data[header_end + 2:]
        sep = '\n'

    header_text = header_bytes.decode('utf-8', errors='replace')
    lines = header_text.split(sep)

    if not lines:
        return None, None, None, None

    # Parse request line: METHOD URL HTTP/1.x
    parts = lines[0].split(' ', 2)
    if len(parts) < 2:
        return None, None, None, None

    method = parts[0].upper()
    url = parts[1]

    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()

    logger.debug("HEADER_KEYS: %s", sorted(headers.keys()))
    return method, url, headers, body_prefix


# ---------------------------------------------------------------------------
# Body readers (caller provides Connection.recv for I/O)
# ---------------------------------------------------------------------------

def read_chunked_body(recv_fn, body_prefix: bytes, timeout: float = 30.0) -> bytes:
    """Parse chunked transfer encoding from a recv function.

    Args:
        recv_fn: callable that takes n and returns bytes (like Connection.recv).
        body_prefix: bytes already read past the header.
        timeout: max seconds to wait for next chunk (unused in sync mode;
                 caller handles via socket timeout).

    Returns:
        Full reassembled body bytes.
    """
    body = bytearray()
    data = body_prefix

    while True:
        # Read until we have the chunk size line
        while b'\r\n' not in data:
            chunk = recv_fn(4096)
            if not chunk:
                return bytes(body)
            data += chunk

        size_end = data.find(b'\r\n')
        size_line = data[:size_end].decode('utf-8', errors='replace').strip()
        data = data[size_end + 2:]

        # Strip chunk extension
        size_line = size_line.split(';')[0].strip()
        try:
            chunk_size = int(size_line, 16)
        except ValueError:
            logger.debug("Invalid chunk size: %r", size_line)
            return bytes(body)

        if chunk_size == 0:
            # Read trailing CRLF after last chunk, then optional trailers
            while b'\r\n\r\n' not in data and b'\n\n' not in data:
                chunk = recv_fn(4096)
                if not chunk:
                    return bytes(body)
                data += chunk
            return bytes(body)

        # Read chunk data + trailing CRLF
        needed = chunk_size + 2
        while len(data) < needed:
            chunk = recv_fn(max(4096, needed - len(data)))
            if not chunk:
                return bytes(body)
            data += chunk

        body.extend(data[:chunk_size])
        data = data[chunk_size + 2:]


def read_content_length_body(recv_fn, body_prefix: bytes, content_length: int) -> bytes:
    """Read exactly content_length bytes from a recv function.

    Args:
        recv_fn: callable like Connection.recv.
        body_prefix: bytes already read past the header.
        content_length: total expected body size.

    Returns:
        Body bytes (may be shorter on EOF/error).
    """
    body = bytearray(body_prefix)
    remaining = content_length - len(body_prefix)

    while remaining > 0:
        chunk = recv_fn(min(65536, remaining))
        if not chunk:
            logger.debug("CL_BODY_EOF: have=%d expected=%d", len(body), content_length)
            return bytes(body)
        body.extend(chunk)
        remaining -= len(chunk)

    return bytes(body[:content_length])


# ---------------------------------------------------------------------------
# Response serialization
# ---------------------------------------------------------------------------

# Headers that must NOT appear in proxy→client response (handled by proxy)
_DROP_RESPONSE = {
    'connection', 'proxy-connection', 'keep-alive',
    'transfer-encoding', 'content-length', 'content-encoding',
}


def build_response_head(status: int, status_text: str, headers: dict,
                        body_len: int, is_chunked: bool = False) -> bytes:
    """Build HTTP/1.1 response header bytes.

    Handles Set-Cookie as array (from Chrome NM) and strips forbidden headers.
    """
    head = f"HTTP/1.1 {status} {status_text}\r\n"
    for k, v in headers.items():
        kl = k.lower()
        if kl == 'set-cookie' and isinstance(v, list):
            for cv in v:
                head += f"Set-Cookie: {cv}\r\n"
        elif kl not in _DROP_RESPONSE:
            head += f"{k}: {v}\r\n"

    if is_chunked:
        head += "Transfer-Encoding: chunked\r\n"
    else:
        head += f"Content-Length: {body_len}\r\n"
    head += "Connection: close\r\n\r\n"
    return head.encode('utf-8')


# ---------------------------------------------------------------------------
# Full request read (combines header parse + body read)
# ---------------------------------------------------------------------------

def read_request(conn) -> Optional[HttpRequest]:
    """Read a complete HTTP request from a Connection.

    Reads header + body (handling chunked, content-length, or EOF modes).
    Returns None on EOF or parse failure.
    """
    # Read header
    data = b''
    while b'\r\n\r\n' not in data and b'\n\n' not in data:
        chunk = conn.recv(4096)
        if not chunk:
            logger.debug("REQUEST_HEADER_EOF: read=%d", len(data))
            return None
        data += chunk
        if len(data) > 65536:
            logger.warning("HEADER_TOO_LARGE")
            return None

    method, url, headers, body_prefix = parse_http_header(data)
    if method is None:
        return None

    # Read body
    body = body_prefix
    transfer_encoding = _get_header(headers, 'Transfer-Encoding').lower()
    content_length_raw = _get_header(headers, 'Content-Length') or None

    if transfer_encoding == 'chunked':
        body = read_chunked_body(conn.recv, body_prefix)
    elif content_length_raw is not None:
        try:
            content_length = int(content_length_raw)
        except ValueError:
            content_length = 0
        body = read_content_length_body(conn.recv, body_prefix, content_length)
    else:
        body = body_prefix if body_prefix else b''

    return HttpRequest(method=method, url=url, headers=headers, body=body)


def write_response(conn, resp: HttpResponse) -> None:
    """Write a complete HTTP response to a Connection."""
    head = build_response_head(resp.status, resp.status_text,
                               resp.headers, len(resp.body))
    try:
        conn.sendall(head + resp.body)
    except OSError as e:
        logger.debug("write_response send error: %s", e)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_header(headers: dict, key: str, default: str = '') -> str:
    """Case-insensitive header lookup."""
    kl = key.lower()
    for k, v in headers.items():
        if k.lower() == kl:
            return v
    return default
