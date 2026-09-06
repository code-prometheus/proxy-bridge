import os, sys
sys.stdout.reconfigure(encoding="utf-8")

# Read clean stable base
with open("local_proxy.py", "rb") as f:
    content = f.read()

# Add raw TLS dump in _mitm_loop — BEFORE any header parsing
# The line we want to insert BEFORE is the call to _read_http_header
old_line = b"\t\t\tmethod, url, headers, body_prefix = _read_http_header(tls_sock)"
new_block = b"""\t\t\t# RAW DUMP: save first 1MB of TLS data for diagnosis
\t\t\ttry:
\t\t\t\ttls_sock.setblocking(True)
\t\t\t\traw = b""
\t\t\t\twhile b"\\r\\n\\r\\n" not in raw and len(raw) < 1048576:
\t\t\t\t\tchunk = tls_sock.recv(4096)
\t\t\t\t\tif not chunk:
\t\t\t\t\t\tbreak
\t\t\t\t\traw += chunk
\t\t\t\t# Read body based on Content-Length if present
\t\t\t\tcl_start = raw.find(b"\\r\\nContent-Length: ")
\t\t\t\tif cl_start < 0:
\t\t\t\t\tcl_start = raw.find(b"\\r\\ncontent-length: ")
\t\t\t\tif cl_start >= 0:
\t\t\t\t\tcl_end = raw.find(b"\\r\\n", cl_start + 18)
\t\t\t\t\tcl_str = raw[cl_start+18:cl_end].decode("ascii", errors="ignore").strip()
\t\t\t\t\tcl = int(cl_str)
\t\t\t\t\theader_end = raw.find(b"\\r\\n\\r\\n")
\t\t\t\t\tbody_start = header_end + 4
\t\t\t\t\talready_read = len(raw) - body_start
\t\t\t\t\tremaining = cl - already_read
\t\t\t\t\twhile remaining > 0:
\t\t\t\t\t\tchunk = tls_sock.recv(min(65536, remaining))
\t\t\t\t\t\tif not chunk:
\t\t\t\t\t\t\tbreak
\t\t\t\t\t\traw += chunk
\t\t\t\t\t\tremaining -= len(chunk)
\t\t\t\ttls_sock.setblocking(False)
\t\t\t\twith open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_request.bin"), "wb") as df:
\t\t\t\t\tdf.write(raw[:1000000])
\t\t\t\tlogger.warning("RAW SAMPLE DUMPED: %d bytes -> sample_request.bin", min(len(raw), 1000000))
\t\t\texcept Exception as e:
\t\t\t\tlogger.debug("Raw dump error: %s", e)
\t\t\t\ttry:
\t\t\t\t\ttls_sock.setblocking(False)
\t\t\t\texcept Exception:
\t\t\t\t\tpass

\t\t\t# Now parse header from the raw data
\t\t\theader_end = raw.find(b"\\r\\n\\r\\n")
\t\t\tif header_end >= 0:
\t\t\t\theader_bytes = raw[:header_end]
\t\t\t\tbody_prefix = raw[header_end + 4:]
\t\t\t\theader_text = header_bytes.decode("utf-8", errors="replace")
\t\t\t\tlines = header_text.split("\\r\\n")
\t\t\t\tif lines:
\t\t\t\t\trequest_line = lines[0]
\t\t\t\t\tparts = request_line.split(" ", 2)
\t\t\t\t\tif len(parts) >= 2:
\t\t\t\t\t\tmethod = parts[0].upper()
\t\t\t\t\t\turl = parts[1]
\t\t\t\t\t\theaders = {}
\t\t\t\t\t\tfor line in lines[1:]:
\t\t\t\t\t\t\tif ":" in line:
\t\t\t\t\t\t\t\tkey, value = line.split(":", 1)
\t\t\t\t\t\t\t\theaders[key.strip()] = value.strip()
\t\t\t\t\telse:
\t\t\t\t\t\tmethod = None
\t\t\telse:
\t\t\t\tmethod = None
\t\t\tif method is None:
\t\t\t\tbreak

\t\t\tbody = body_prefix"""

content = content.replace(old_line, new_block)

# Remove the old body reading logic after the header parse (it's now handled in the raw dump)
# Actually need to remove the original body read block — it comes AFTER the old _read_http_header call
# Let me find and remove it
old_body_block = b"""\t\t\tbody = body_prefix
\t\t\ttransfer_encoding = headers.get(\"Transfer-Encoding\", \"\").lower()
\t\t\tcontent_length_raw = headers.get(\"Content-Length\")

\t\t\tif transfer_encoding == \"chunked\":
\t\t\t\tbody = _read_chunked_body(tls_sock, body_prefix)
\t\t\telif content_length_raw is not None:
\t\t\t\ttry:
\t\t\t\t\tbody = _read_content_length_body(tls_sock, body_prefix, int(content_length_raw))
\t\t\t\texcept ValueError:
\t\t\t\t\tbody = body_prefix if body_prefix else b\"\"
\t\t\telse:
\t\t\t\tbody = body_prefix if body_prefix else b\"\""""

# Replace with a simplified version that just reads remaining body
simple_body = b"""\t\t\t# Raw dump already read the full body — body_prefix has it all
\t\t\tcontent_length_raw = headers.get(\"Content-Length\")
\t\t\tif content_length_raw:
\t\t\t\ttry:
\t\t\t\t\texpected = int(content_length_raw)
\t\t\t\t\twhile len(body_prefix) < expected:
\t\t\t\t\t\tchunk = tls_sock.recv(min(65536, expected - len(body_prefix)))
\t\t\t\t\t\tif not chunk:
\t\t\t\t\t\t\tbreak
\t\t\t\t\t\tbody_prefix += chunk
\t\t\t\t\tbody = body_prefix[:expected]
\t\t\t\texcept ValueError:
\t\t\t\t\tbody = body_prefix
\t\t\telse:
\t\t\t\tbody = body_prefix"""

content = content.replace(old_body_block, simple_body)

with open("local_proxy.py", "wb") as f:
    f.write(content)
print("Raw TLS dump added — will save sample_request.bin on next big request")
