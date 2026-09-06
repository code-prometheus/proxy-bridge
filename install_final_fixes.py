import sys
import os

# Read clean base
with open("local_proxy.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: logger name
content = content.replace(
    "logger = logging.getLogger(__name__)",
    "logger = logging.getLogger(\"proxy_bridge.local_proxy\")"
)

# Fix 2: header limit 65536 -> 1048576
content = content.replace("65536", "1048576")

# Fix 3: Chrome-forbidden headers in _forward_via_nm
content = content.replace(
    'drop_request = {"connection", "proxy-connection", "keep-alive", "host"}\n\tfor k, v in headers.items():',
    'drop_request = {"connection", "proxy-connection", "keep-alive", "host", "content-length", "transfer-encoding", "content-encoding", "accept-encoding"}\n\tfor k, v in headers.items():',
    1  # first occurrence only (_forward_via_nm)
)

# Fix 4: Chrome-forbidden headers in _forward_via_urllib (2nd occurrence)
content = content.replace(
    'drop_request = {"connection", "proxy-connection", "keep-alive", "host"}\n\tfor k, v in headers.items():',
    'drop_request = {"connection", "proxy-connection", "keep-alive", "host", "content-length", "transfer-encoding", "content-encoding", "accept-encoding"}\n\tfor k, v in headers.items():'
)

# Fix 5: Add raw dump + body size log in _mitm_loop
content = content.replace(
    '\t\tlogger.debug("MITM request: %s %s", method, full_url)',
    '\t\tlogger.debug("MITM request: %s %s body=%d", method, full_url, len(body))\n'
    '\t\tif len(body) > 100000:\n'
    '\t\t\twith open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_request.bin"), "wb") as sf:\n'
    '\t\t\t\tsf.write(body)\n'
    '\t\t\tlogger.warning("SAMPLE DUMPED: %d bytes to sample_request.bin", len(body))'
)

with open("local_proxy.py", "w", encoding="utf-8") as f:
    f.write(content)

print("5 fixes applied")
