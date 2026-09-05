# localhost:60130 代理大 Body 截断问题诊断报告

## 一、现象

holoProxy (Rust reqwest) 通过 `http://localhost:60130` 代理访问 `https://integrate.api.nvidia.com/v1/chat/completions` 时，大请求体（>600KB）间歇性失败：

**第 1 次请求**：HTTP 500，NVIDIA 报错：
```
failed to decode json body: json: string of object unexpected end of JSON input
```
即 NVIDIA 服务端收到的 JSON 被截断，不是完整请求体。

**后续重试**：HTTP 502（代理/上游直接关闭连接，0 字节响应体）。

## 二、诊断数据

### 2.1 holoProxy 侧验证

- **序列化完整性**：本地序列化的 JSON 最后 50 字节为 `,"type":"function"}]}`，根对象和 tools 数组均正确闭合。
- **body 大小**：实际请求约 678KB-808KB（取决于对话历史长度）。
- **小 body 正常**：~200B 的简单请求永远成功。

### 2.2 客户端对比测试

| 客户端 | 协议 | Body 大小 | 结果 |
|--------|------|-----------|------|
| Python urllib | HTTP/1.1 | 586KB | ✅ 200 |
| Python urllib | HTTP/1.1 | 626KB | ✅ 200 |
| Python urllib | HTTP/1.1 | 684KB | ❌ 502 |
| Python urllib | HTTP/1.1 | 723KB | ✅ 200 |
| Python urllib | HTTP/1.1 | 782KB | ✅ 200 |
| reqwest `.http1_only()` | HTTP/1.1 | 808KB | ❌ 500 |
| reqwest `.http1_only()` | HTTP/1.1 | 808KB | ❌ 502 |
| 原始 TCP socket | HTTP/1.1 | 801KB | ❌ 0 字节响应 |

### 2.3 原始 TCP 验证

直接用 Python socket 建立 CONNECT 隧道，手动 TLS 握手，用 `socket.sendall()` 一次性发送完整 801KB HTTP 请求 → **上游 0 字节响应**。

### 2.4 排除项

| 怀疑方向 | 验证方式 | 结论 |
|----------|----------|------|
| HTTP/2 DATA 帧截断 | Python urllib（HTTP/1.1）也失败 | ❌ 排除 |
| reqwest 特定 bug | urllib 同样失败 | ❌ 排除 |
| SSL/TLS 问题 | 原始 socket + 手动 TLS 失败 | ❌ 排除 |
| 固定大小阈值 | 684KB 失败但 723KB/782KB 成功 | ❌ 排除 |
| 代理新修复已解决 | 修复后同样失败 | ❌ 排除 |

## 三、判定

**问题在代理的 TCP 字节转发层。**

代理以 MITM 模式工作：与客户端做 TLS 终止 → 解析 HTTP 请求 → 向上游重新发起 HTTPS 连接。在转发 body 到上游的过程中，大 body 的字节没有被完整写出。

## 四、推测根因

MITM 模式下代理对 POST body 的转发是**逐 chunk 读取并写出**。关键代码模式：

```
while remaining > 0:
    chunk = client_socket.recv(min(bufsize, remaining))
    upstream_socket.sendall(chunk)
    remaining -= len(chunk)
```

或者使用 `asyncio` 的 `reader.read(n)` + `writer.write(data)` + `writer.drain()`。

可能的故障点：

### 4.1 `sendall()` 异常未捕获

Python 的 `socket.sendall()` 在发送缓冲区满时可能抛出 `BlockingIOError`（非阻塞模式）或 `BrokenPipeError`。如果异常被静默吞掉或触发 `break` 退出循环，剩余字节永远不会到达上游。

### 4.2 `asyncio.StreamWriter.drain()` 时序问题

如果使用 asyncio，`writer.write(data)` 只是放入缓冲区，`await writer.drain()` 确保发送。如果 drain 超时或被取消，缓冲区中的数据可能丢失，但外层循环以为已发送完毕。

### 4.3 上游连接提前关闭

上游 NVIDIA 服务器在收到不完整 JSON 后可能发送 TCP RST。代理的 `sendall()` 遇到 `BrokenPipeError` 后退出写入循环，但此时 body 还没发完。问题是**第一次为什么 JSON 就不完整** — 即写入循环本身就有缺陷。

### 4.4 buffer size 边界

Python `socket.recv(BUFSIZE)` 不保证返回 BUFSIZE 字节。在大消息的场景中，如果接收端用固定大小缓冲区且未正确处理 `len(chunk) < expected` 的情况，会导致提前认为 body 读完。

## 五、验证建议

在代理的 body 转发代码中添加日志：

```python
total_read = 0
total_sent = 0
while remaining > 0:
    chunk = client_sock.recv(min(BUFSIZE, remaining))
    if not chunk:
        logging.error(f"BODY READ EOF: read={total_read}, remaining={remaining}")
        break
    total_read += len(chunk)
    try:
        upstream_sock.sendall(chunk)
        total_sent += len(chunk)
    except Exception as e:
        logging.error(f"BODY SEND ERROR: sent={total_sent}, remaining={remaining}, err={e}")
        break
    remaining -= len(chunk)

logging.info(f"BODY FORWARD DONE: read={total_read}, sent={total_sent}")
if total_read != content_length:
    logging.error(f"BODY TRUNCATED: expected={content_length}, actual={total_sent}")
```

用 800KB body 触发错误后检查日志确认 `total_sent` 是否等于 `content_length`。
