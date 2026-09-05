# Proxy Bridge v2.0 — CLAUDE.md

## 架构总览

```
任意本地程序 → 127.0.0.1:60130 (HTTP/CONNECT 代理)
  ├─ 普通 HTTP 请求 → [Chrome NM] 或 [urllib fallback] → 互联网
  └─ CONNECT (https://) → MITM TLS 终止 → 解密 HTTP → [Chrome NM] 或 [urllib fallback] → 互联网
```

**核心依赖**：Chrome 扩展 (MV3) 通过 Native Messaging 连接 Python 代理，代理用 Chrome 的 `fetch()` API 执行所有上游请求。

---

## 铁律（最高优先级，不可妥协！）

### 🔒 1. SSL 证书信任 — MITM 模式

**代理自行终止 TLS。客户端 NEVER 接触目标服务器证书。**

机制：
- CONNECT 请求进来 → 代理返回 `200 Connection Established`
- 代理用本地 CA 动态签发 per-host 证书，`ssl.wrap_socket(server_side=True)` 接管 TLS
- 客户端 TLS 握手对方是代理，看到的是 `CN=目标域名, issuer=Proxy Bridge Local CA`
- 客户端需信任本地 CA（`python entry.py --install-ca` 一次）
- 代理解密 HTTP 后走 Chrome NM (`_forward_via_nm`) 或 urllib (`_forward_via_urllib`) 转发
- Chrome NM 路径：Chrome fetch + ghelper 自动处理上游 SSL（翻墙 + 证书验证）
- urllib 路径：直接 `urllib.request.urlopen`（不走翻墙，仅用于无 Chrome 时的 fallback）

**验证命令**（不带 `-k`，必须通过）：
```
curl -x http://127.0.0.1:60130 https://platform.worldquantbrain.com/sign-in
curl -x http://127.0.0.1:60130 https://www.google.com
```

**不要回退到 TCP 隧道模式！** 旧版 `_tunnel_via_nm` / `_tunnel_via_direct` 已删除。那些函数尝试 `socket.connect(host, port)` 直连目标，在中国会因防火墙失败。MITM 模式是唯一正确的实现。

### 🚀 2. Chrome 拉起

- **代理只由 Chrome 扩展通过 NM 启动** — Chrome 启动 → 扩展连接 NM → Python 进程启动
- Chrome 关闭时 NM stdin EOF → `os._exit(0)` 自动杀死 Python 进程
- 端口绑定用 `SO_EXCLUSIVEADDRUSE`（Windows 必须），绝不允许双开

### 🧵 3. NM 协议

Python ↔ Chrome 扩展通过 stdin/stdout 的 length-prefixed JSON 通信。

消息类型：
- `request_start` → `request_chunk`* → `request_end` （Python → Chrome）
- `response` → `chunk`* → `end` 或 `error` （Chrome → Python）

每条消息带 `id` 字段用于多路复用。**不要改变消息格式。** Python 端 `utils.nm_pending_requests` 用 `id` 路由响应。

### 🚫 4. HTTP CONNECT 处理 — 绝不拒绝

CONNECT 请求 **永远返回 200**，不走 405。客户端拿到 200 后发起 TLS，代理做 MITM 终止。

**不要**改回 405 拒绝让客户端降级。代理自己就是 TLS endpoint。

---

## 文件职责

| 文件 | 职责 | 不可动 |
|------|------|--------|
| `entry.py` | 入口：CA 管理 (`--init-ca`/`--install-ca`)、启动 proxy server + NM bridge | NM bridge 必须在主线程 |
| `local_proxy.py` | 代理核心：MITM TLS 终止、HTTP 转发（NM/urllib）、NM reader/writer | `_forward_via_nm`、`_connect_mitm` |
| `utils.py` | CertManager（CA + per-host 证书）、NM 队列、HTTP 解析 | CertManager API |
| `extension/` | Chrome MV3 扩展（background.js 处理 NM 消息 + fetch） | 消息格式 |

### local_proxy.py 关键函数

| 函数 | 职责 |
|------|------|
| `handle_client` | 入口：解析首行 → CONNECT 走 MITM，其他走 HTTP handler |
| `handle_connect_tunnel` | CONNECT 入口：发送 200 → `_connect_mitm` |
| `_connect_mitm` | 用 per-host 证书创建 TLS server socket → `_mitm_loop` |
| `_mitm_loop` | 在 TLS socket 上循环读 HTTP 请求 → `_forward_via_nm` 或 `_forward_via_urllib` |
| `_forward_via_nm` | 通过 NM 发送请求到 Chrome，流式读回响应（支持 chunked） |
| `_forward_via_urllib` | Fallback：urllib 直连（仅无 Chrome 时） |
| `handle_http_request` | 处理非 CONNECT 的 HTTP 请求 |
| `native_reader_thread` | 读 stdin NM 消息 → 路由到 `nm_pending_requests` |
| `native_writer_thread` | 取 `nm_send_queue` → 写 length-prefixed JSON 到 stdout |

### utils.py 关键 API

| 项目 | 说明 |
|------|------|
| `CertManager.get_ca()` | 生成/加载根 CA 证书（`~/.proxy-bridge-ca/ca-cert.pem`） |
| `CertManager.get_cert_for_host(host)` | 为 host 签发证书（由 CA 签名），自动缓存 |
| `CertManager.install_ca_to_system()` | Windows certutil 安装 CA 到系统信任存储 |
| `nm_send_queue` | `queue.Queue()` — 用于向 Chrome 发送 NM 消息 |
| `nm_pending_requests` | `dict[int, callable]` — id → handler 映射 |
| `CHROME_CONNECTED` | `bool` — Chrome NM 是否已连接 |
| `LOCAL_PROXY_PORT` | 默认 `60130`（从 `settings.json` 读取） |

---

## 故障排查

### 代理启动失败（端口被占用）
```powershell
netstat -ano | findstr 60130   # 找到 PID
taskkill /PID <pid> /F         # 杀掉
```

### 客户端 SSL 证书错误
确保 CA 已安装：`python entry.py --install-ca`（管理员权限）

### Google 等被墙站点 502
检查 Chrome NM 是否连接（`CHROME_CONNECTED = True`）。如果手动启动（无 Chrome），urllib fallback 直连被墙 — 需用 Chrome 扩展正常启动。

---

## 已验证通过的测试（2026-09-05）

```
✅ curl -x http://127.0.0.1:60130 https://platform.worldquantbrain.com/sign-in
   HTTP 200 | SSL verify: 0 (通过) | 0.98s | issuer: Proxy Bridge Local CA

✅ curl -x http://127.0.0.1:60130 https://www.google.com
   HTTP 200 | SSL verify: 0 (通过) | 1.80s | issuer: Proxy Bridge Local CA
```

两个测试均 **不带 `-k`**，证书由 `CN=Proxy Bridge Local CA` 签发，curl 完全信任。
