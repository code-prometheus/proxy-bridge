"""
Proxy Bridge — Connection abstraction.
Single owner of a socket's lifecycle: read, write, shutdown, close.
Supports both plain and TLS-wrapped sockets transparently.
"""
import socket
import ssl
import logging

logger = logging.getLogger('proxy_bridge.connection')


class Connection:
    """Wrapper around a raw or TLS socket that owns the full lifecycle.

    Rules:
    - This is the ONLY place that calls sock.close().
    - shutdown() sends SHUT_WR to flush buffers before close.
    - close() is idempotent (internal _closed flag).
    - Supports context manager: `with Connection(sock) as conn: ...`
    """

    def __init__(self, sock: socket.socket, tls_ctx: ssl.SSLContext = None):
        self._raw = sock
        self._tls = None
        self._closed = False

        if tls_ctx:
            try:
                self._tls = tls_ctx.wrap_socket(sock, server_side=True)
                self._tls.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._tls.settimeout(300)
            except Exception:
                logger.debug("TLS handshake failed")
                self._closed = True
                raise
        else:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(300)

    @property
    def sock(self):
        """The active socket (TLS if wrapped, otherwise raw)."""
        return self._tls if self._tls else self._raw

    @property
    def is_tls(self) -> bool:
        return self._tls is not None

    def recv(self, n: int) -> bytes:
        """Read up to n bytes. Returns empty bytes on EOF."""
        try:
            return self.sock.recv(n)
        except (ssl.SSLWantReadError, BlockingIOError):
            return b''
        except Exception as e:
            logger.debug("Connection recv error: %s", e)
            return b''

    def sendall(self, data: bytes) -> None:
        """Send all data. Raises on socket error."""
        self.sock.sendall(data)

    def shutdown(self) -> None:
        """Send SHUT_WR to signal end of writes, flush buffers.

        Called before close() to ensure all buffered data reaches the peer.
        Safe to call on already-closed sockets.
        """
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except (OSError, ssl.SSLError):
            pass  # already closed or not connected

    def close(self) -> None:
        """Idempotent close. Shuts down TLS cleanly if wrapped."""
        if self._closed:
            return
        self._closed = True

        if self._tls:
            try:
                self._tls.unwrap()
            except (ssl.SSLError, OSError):
                pass
            try:
                self._raw.close()
            except OSError:
                pass
        else:
            try:
                self._raw.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        self.close()
        return False
