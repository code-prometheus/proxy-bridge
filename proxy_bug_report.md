# localhost:60130 代理 HTTP/2 大 Body 截断 Bug 报告

## 现象

Rust reqwest 客户端通过 `http://localhost:60130` 代理（HTTPS CONNECT 隧道）发送 >600KB 的 HTTP POST body 到 `https://integrate.api.nvidia.com/v1/chat/completions` 时，NVIDIA 服务端返回：

```
HTTP 500: "failed to decode json body: json: string of object unexpected end of JSON input"
```

即服务端收到的 JSON 在中间某处被截断，不是完整的请求体。

## 已验证事实

| 测试场景 | Body 大小 | 协议 | 结果 |
|---------|----------|------|------|
| Python urllib + 代理 | 600KB | HTTP/1.1 | ✅ 200 OK |
| reqwest + 代理（默认）| 678KB | HTTP/2 (h2) | ❌ 500 "unexpected end of JSON" |
| reqwest + 代理（`.http1_only()`）| 739KB | HTTP/1.1 | ✅ 200 OK（偶尔需要 retry）|
| reqwest + 代理（默认）| ~200B | HTTP/2 (h2) | ✅ 200 OK |

**结论：问题只在 HTTP/2 + 代理 + 大 body（>~64KB）时出现。**

## 根因分析

### 客户端侧已排除的问题

1. **JSON 完整性**：reqwest 序列化后的 body 本地完整闭合，最后 50 字节为 `,"type":"function"}]}`（根对象正确闭合）
2. **Content-Type**：正确设置为 `application/json`
3. **TLS 证书**：`danger_accept_invalid_certs(true)`，且代理端不需要验证上游证书

### 高度怀疑的代理端问题

HTTP/2 CONNECT 隧道的大 body 传输涉及以下环节，任一环节出错都会导致截断：

**1. HTTP/2 DATA 帧分片未完整转发**
- HTTP/2 客户端将大 body 分成多个 DATA 帧（每帧最大 16384 字节）
- 代理从客户端读取 DATA 帧，通过 CONNECT 隧道的 TCP socket 写给上游
- 如果代理在 **所有 DATA 帧到达之前** 就发送了 `END_STREAM` flag，上游就会收到截断的 body

**2. HTTP/2 Flow Control 窗口更新 (WINDOW_UPDATE) 丢失**
- HTTP/2 有连接级和流级的流量控制窗口（初始 65535 字节）
- 对于 >64KB 的 body，客户端必须等待代理发送 WINDOW_UPDATE 帧才能继续发送
- 如果代理消费了 DATA 帧但没有及时发送 WINDOW_UPDATE，客户端会阻塞
- 如果代理在超时后关闭了 stream，body 就会不完整

**3. Stream 半关闭 (half-close) 时序错误**
- HTTP/2 客户端发送完所有 DATA 帧后，发送 `END_STREAM` flag
- 代理收到 `END_STREAM` 后应该关闭上游 socket 的写端（发送 TCP FIN 或 shutdown(SHUT_WR)）
- 但如果代理在读取完所有 DATA 帧 **之前** 就关闭了上游 socket，就会截断

**4. Buffer 大小限制**
- 代理内部用于读取/转发 body 的 buffer 可能有固定大小限制
- 如果 buffer 只分配了 64KB 或类似值，超过的部分会被丢弃
- 检查代理代码中的 `buf.read()` / `copy_buf()` 是否在循环中多次调用直到 EOF

## 建议的排查步骤

1. **加日志**：在代理的 HTTP/2 → 上游 TCP 转发路径中，记录：
   - 从客户端接收到每个 DATA 帧的大小和 flags（特别是 END_STREAM）
   - 写入上游 socket 的字节数和返回值
   - `tokio::io::copy` 或手动循环的写出总字节数

2. **最小复现**：用 curl 强制 HTTP/2 测试：
   ```bash
   curl --http2 -x http://localhost:60130 \
     -H "Content-Type: application/json" \
     -d "$(python -c 'print("x"*200000)')" \
     https://integrate.api.nvidia.com/v1/chat/completions
   ```
   如果 curl HTTP/2 也失败，就是代理的通用 bug；如果只有 reqwest 失败，可能是 h2 crate 实现差异。

3. **抓包对比**：用 Wireshark 分别抓取 HTTP/1.1 和 HTTP/2 两种模式的代理流量，对比 DATA 帧数量和上游 TCP 写出量。

## 临时绕过方案（holoProxy 已采用）

在 reqwest Client builder 中强制 HTTP/1.1：
```rust
let client = Client::builder()
    .http1_only()
    .danger_accept_invalid_certs(true)
    // ...
    .build()?;
```

这使得 739KB body 能正常通过代理到达 NVIDIA API。但 HTTP/1.1 失去了 HTTP/2 的 multiplexing 优势，对并发请求有性能影响，应作为临时方案。
