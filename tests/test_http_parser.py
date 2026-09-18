"""
Unit tests for http_parser.py — pure functions, no I/O required.
Tests parse_http_header, read_chunked_body, read_content_length_body,
build_response_head, and read_request.
"""
import io
import os
import sys
import unittest

# Add parent dir to path so we can import the modules
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PARENT)

import http_parser
from http_parser import (parse_http_header, read_chunked_body,
                         read_content_length_body, build_response_head)


class TestParseHttpHeader(unittest.TestCase):
    """parse_http_header — pure function, no I/O."""

    def test_simple_get(self):
        raw = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'GET')
        self.assertEqual(url, '/')
        self.assertEqual(headers, {'Host': 'example.com'})
        self.assertEqual(body_prefix, b'')

    def test_post_with_body_prefix(self):
        raw = b"POST /api/data HTTP/1.1\r\nHost: api.example.com\r\nContent-Length: 5\r\n\r\nhello"
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'POST')
        self.assertEqual(url, '/api/data')
        self.assertEqual(headers['Content-Length'], '5')
        self.assertEqual(body_prefix, b'hello')

    def test_multiple_headers(self):
        raw = (b"GET /path?q=1 HTTP/1.1\r\n"
               b"Host: test.com\r\n"
               b"Accept: application/json\r\n"
               b"User-Agent: curl/8.0\r\n"
               b"\r\n")
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'GET')
        self.assertEqual(url, '/path?q=1')
        self.assertEqual(headers['Host'], 'test.com')
        self.assertEqual(headers['Accept'], 'application/json')
        self.assertEqual(headers['User-Agent'], 'curl/8.0')

    def test_connect_method(self):
        raw = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'CONNECT')
        self.assertEqual(url, 'example.com:443')
        self.assertEqual(headers['Host'], 'example.com:443')

    def test_lf_only_separator(self):
        raw = b"GET / HTTP/1.1\nHost: example.com\n\n"
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'GET')
        self.assertEqual(headers['Host'], 'example.com')

    def test_incomplete_header(self):
        raw = b"GET / HTTP/1.1\r\nHost: ex"
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertIsNone(method)

    def test_empty_data(self):
        raw = b""
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertIsNone(method)

    def test_oversized_header(self):
        raw = b'x' * 65537
        method, url, headers, body_prefix = parse_http_header(raw)
        self.assertEqual(method, 'TOO_LARGE')


class TestReadChunkedBody(unittest.TestCase):
    """read_chunked_body — pure function, takes a recv_fn mock."""

    def test_single_chunk(self):
        data = b"5\r\nhello\r\n0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'')
        self.assertEqual(body, b'hello')

    def test_multiple_chunks(self):
        data = b"3\r\nhel\r\n2\r\nlo\r\n0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'')
        self.assertEqual(body, b'hello')

    def test_body_prefix(self):
        """body_prefix starts with chunk size line, recv provides rest."""
        # Prefix: "5\r\nhel" (chunk size + partial data)
        # Recv returns: "lo\r\n0\r\n\r\n" (rest of chunk + terminator)
        data = b"lo\r\n0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'5\r\nhel')
        self.assertEqual(body, b'hello')

    def test_chunk_extension_ignored(self):
        data = b"5;ext=ignored\r\nhello\r\n0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'')
        self.assertEqual(body, b'hello')

    def test_empty_body(self):
        data = b"0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'')
        self.assertEqual(body, b'')

    def test_eof_during_chunk(self):
        """Partial chunk data, then recv returns empty (EOF) → returns empty."""
        data = b""
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'9\r\nhel')
        # Partial "hel" is in the buffer but never committed to body
        # before EOF — known edge case, returns b''
        self.assertEqual(body, b'')

    def test_invalid_chunk_size(self):
        data = b"XYZ\r\nhello\r\n0\r\n\r\n"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_chunked_body(recv_fn, b'')
        self.assertEqual(body, b'')  # invalid hex → bail out


class TestReadContentLengthBody(unittest.TestCase):
    """read_content_length_body — pure function, takes a recv_fn mock."""

    def test_exact_length(self):
        data = b"hello"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_content_length_body(recv_fn, b'', 5)
        self.assertEqual(body, b'hello')

    def test_with_prefix(self):
        data = b"world"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_content_length_body(recv_fn, b'hello', 10)
        self.assertEqual(body, b'helloworld')

    def test_eof_before_full(self):
        data = b"hel"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_content_length_body(recv_fn, b'', 10)
        self.assertEqual(body, b'hel')  # EOF, return what we have

    def test_longer_than_needed(self):
        data = b"helloworldextra"
        buf = io.BytesIO(data)

        def recv_fn(n):
            return buf.read(n)

        body = read_content_length_body(recv_fn, b'', 5)
        self.assertEqual(body, b'hello')  # truncated to content_length

    def test_zero_length(self):
        body = read_content_length_body(lambda n: b'', b'', 0)
        self.assertEqual(body, b'')


class TestBuildResponseHead(unittest.TestCase):
    """build_response_head — pure function, builds HTTP response bytes."""

    def test_basic_response(self):
        head = build_response_head(200, 'OK', {'Content-Type': 'text/html'}, 42)
        self.assertIn(b'HTTP/1.1 200 OK', head)
        self.assertIn(b'Content-Length: 42', head)
        self.assertIn(b'Content-Type: text/html', head)
        self.assertIn(b'Connection: close', head)

    def test_set_cookie_array(self):
        head = build_response_head(200, 'OK',
                                   {'Set-Cookie': ['a=1', 'b=2']}, 0)
        self.assertIn(b'Set-Cookie: a=1', head)
        self.assertIn(b'Set-Cookie: b=2', head)

    def test_drops_forbidden_headers(self):
        head = build_response_head(200, 'OK', {
            'Content-Type': 'text/html',
            'Transfer-Encoding': 'chunked',
            'Content-Encoding': 'gzip',
            'Connection': 'keep-alive',
            'Proxy-Connection': 'keep-alive',
            'Keep-Alive': 'timeout=5',
        }, 100)
        self.assertIn(b'Content-Type: text/html', head)
        self.assertNotIn(b'Transfer-Encoding', head)
        self.assertIn(b'Content-Encoding', head)  # NOW preserved — pass through upstream encoding
        self.assertNotIn(b'Proxy-Connection', head)
        self.assertNotIn(b'Keep-Alive', head)
        # Connection: close is always added
        self.assertIn(b'Connection: close', head)

    def test_chunked_mode(self):
        head = build_response_head(200, 'OK', {}, 0, is_chunked=True)
        self.assertIn(b'Transfer-Encoding: chunked', head)
        self.assertNotIn(b'Content-Length', head)


class TestReadRequest(unittest.TestCase):
    """read_request — uses Connection.recv, test with mock Connection."""

    class MockConn:
        """Mock Connection that reads from a BytesIO buffer."""
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)
            self._recv_count = 0

        def recv(self, n: int) -> bytes:
            self._recv_count += 1
            return self._buf.read(n)

    def test_simple_get_request(self):
        data = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
        conn = self.MockConn(data)
        req = http_parser.read_request(conn)
        self.assertIsNotNone(req)
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.url, '/')
        self.assertEqual(req.headers['Host'], 'example.com')
        self.assertEqual(req.body, b'')

    def test_post_with_content_length(self):
        data = (b"POST /api HTTP/1.1\r\n"
                b"Host: api.example.com\r\n"
                b"Content-Length: 5\r\n"
                b"\r\n"
                b"hello")
        conn = self.MockConn(data)
        req = http_parser.read_request(conn)
        self.assertIsNotNone(req)
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.body, b'hello')

    def test_chunked_post(self):
        data = (b"POST /upload HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"5\r\nhello\r\n"
                b"0\r\n\r\n")
        conn = self.MockConn(data)
        req = http_parser.read_request(conn)
        self.assertIsNotNone(req)
        self.assertEqual(req.body, b'hello')

    def test_eof_during_header(self):
        conn = self.MockConn(b"GET / HT")
        req = http_parser.read_request(conn)
        self.assertIsNone(req)

    def test_no_body_request(self):
        data = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
        conn = self.MockConn(data)
        req = http_parser.read_request(conn)
        self.assertIsNotNone(req)
        self.assertEqual(req.body, b'')


if __name__ == '__main__':
    unittest.main()
