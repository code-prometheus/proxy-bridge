# Proxy Bridge v1.0.0 — CLAUDE.md

## 架构总览

```
任意本地程序 → 127.0.0.1:60130 (HTTP/CONNECT 代理)
 ├─ 普通 HTTP 请求 → [Chrome NM] → 互联网
 └─ CONNECT (https://) → MITM TLS 终止 → 解密 HTTP → [Chrome NM] → 互联网
```

**核心依赖**：Chrome 扩展 (MV3) 通过 Native Messaging 连接 Python 代理，代理用 Chrome 的 `fetch()` API 执行所有上游请求。

## 文件职责

| 文件 | 职责 |
|------|------|
| `entry.py` | 入口：CA 管理 (`--init-ca`/`--install-ca`)、启动 proxy server + NM bridge |
| `proxy.py` | 代理核心：MITM TLS 终止、HTTP/CONNECT 路由、连接池 |
| `nm.py` | Chrome NM 传输层：length-prefixed JSON stdin/stdout、O(1) dispatch |
| `upstream.py` | 上游转发：队列流式传输 NM chunks → 客户端（chunked TE） |
| `certs.py` | 证书管理：根 CA 生成 + per-host 动态签发 |
| `http_parser.py` | HTTP/1.1 解析：header parse、chunked/CL body read、响应构建 |
| `connection.py` | 连接抽象：统一 raw/TLS socket 生命周期、shutdown + close |
| `utils.py` | 配置加载 (`settings.json` → `LOCAL_PROXY_PORT`)、日志初始化 |
| `AutoSetup.py` | Windows 一键安装（pip install、CA 生成、NM 注册表） |
| `extension/` | Chrome MV3 扩展 (background.js Service Worker + popup) |

---

## 铁律（不可妥协）

### 0. Chrome fetch() — 上游唯一转发路径

```
✅ Chrome fetch() → 唯一正确路径
❌ urllib.request.urlopen → 仅 CHROME_CONNECTED=False 时紧急降级
❌ socket.connect / TCP 隧道 → 永不复活
```

### 1. SSL 证书信任 — MITM 模式

- 代理自行终止 TLS（`ssl.wrap_socket(server_side=True)`）
- 客户端接触的是 `CN=目标域名, issuer=Proxy Bridge Local CA`
- 客户端需信任本 CA（`python entry.py --install-ca`）
- 代理解密 HTTP 后走 Chrome NM 转发

### 2. Chrome 拉起 + 端口 60130 生命周期

- **AutoSetup** → `step_kill_proxy_port()` PID 精准杀旧进程
- **proxy.py** → `SO_EXCLUSIVEADDRUSE` + bind 失败 `os._exit(0)` 防重复
- **nm.py** → Chrome NM 断开 → `shutdown_event.set()` → accept loop 退出 → `sys.exit(0)` → 端口释放

### 3. NM 协议

Python ↔ Chrome 扩展通过 stdin/stdout 的 length-prefixed JSON 通信：

| Python → Chrome | Chrome → Python |
|-----------------|-----------------|
| `request_start` → `request_chunk`* → `request_end` | `response` → `chunk`* → `end` 或 `error` |

每条消息带 `id` 多路复用。`nm_pending_requests[id] = handler` 实现 O(1) dispatch。

### 4. 流式传输 — 不缓冲 body

`_forward_via_nm` 使用 `queue.Queue` 流式转发 NM chunks：
- 收到 `response` → 立即发送 chunked TE header
- 收到 `chunk` → 立即发送 `hex-size + CRLF + data + CRLF`
- 收到 `end` → 发送 `0\r\n\r\n`

**body 从不全量缓冲内存**，支持任意大小文件下载。

### 5. Content-Encoding 安全

Chrome `fetch()` 透明解压所有编码（gzip/deflate/brotli），但 header 仍保留原始值。
`_maybe_decompress` 无条件剥离响应头中的 `Content-Encoding`，防止客户端对明文 body 再次解压。

### 6. Header 安全

Chrome Fetch API 禁止手动设置的头（会导致 TypeError）：
`Content-Length`, `Transfer-Encoding`, `Accept-Encoding` 等。
Python `upstream.py` 和 `extension/background.js` 的 `filterRequestHeaders` 同步过滤。

---

## 故障排查

### 代理启动失败
```powershell
netstat -ano | findstr 60130
taskkill /F /PID <pid>
```

### 客户端 SSL 错误
```bash
python entry.py --install-ca  # 管理员权限
```

### Chrome fetch "Failed to fetch"
1. 检查 `Content-Length`/`Transfer-Encoding` 是否已过滤
2. `chrome://extensions` → Proxy Bridge → `service worker` → Console 诊断

---

## 验证命令

```bash
# 不带 -k，依赖 CA 信任
curl -x http://127.0.0.1:60130 https://httpbin.org/get
curl -x http://127.0.0.1:60130 https://www.google.com
```

---

## Build / Release

```bash
# tag push 触发 build-release.yml
git tag v1.0.0 && git push origin v1.0.0
# → GitHub Actions: windows-latest PyInstaller → ProxyBridge-Setup.exe → GitHub Release
```
