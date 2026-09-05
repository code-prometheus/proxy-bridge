# Kimi K3 代理故障分析报告

**日期**: 2026-09-05
**现象**: holoProxy 通过 `127.0.0.1:60130` 代理访问 NVIDIA Kimi K3 (`integrate.api.nvidia.com`) 时，每次请求立即返回 HTTP 502（`in=0.0s`），重试 3 次后全部耗尽，Claude Code 进入 Recovery 循环。

## 关键数据点

### 1. 代理基本连通性
- `curl GET /v1/models` 通过代理 → **200 OK**（正常）
- `curl POST /v1/chat/completions` 小请求 (100B body) 通过代理 → **200 OK**，但耗时 **30 秒**
- `curl POST` 非流式 (`stream:false`) → **超时**
- holoProxy POST (~400KB body, 1167 messages) → **502 in=0.0s**

### 2. holoProxy 日志（典型失败链路）
```
22:31:43 INFO [req msg_1788618703000] agent=true msgs=1167 tok=213477 ... moonshotai/kimi-k3
22:31:43 INFO [msg_1788618703000] attempt=1 http=502 in=0.0s
22:31:43 WARN [msg_1788618703000] HTTP 502 error body (0B):
22:31:43 INFO [msg_1788618703000] attempt=2 http=502 in=0.0s
22:31:43 INFO [msg_1788618703000] attempt=3 http=502 in=0.1s
22:31:43 WARN [msg_1788618703000] ALL 3 retries exhausted after 0.1s: HTTP 502
```

### 3. 对比：DeepSeek 直连正常
```
22:58:10 INFO [msg_1788620290534] attempt=1 http=200 in=0.5s  (DeepSeek, 内网直连, 430KB body)
```

### 4. 代理 TCP 连接状态
```
TCP 127.0.0.1:60130 → LISTENING (PID 12452, python)
6x ESTABLISHED connections
5x TIME_WAIT connections
```

## 根因分析

### 问题不在 holoProxy 侧

1. **holoProxy 的 `use_proxy` 机制已正确实现**：`client.rs` 使用 `reqwest::Proxy::all()` 设置代理，DeepSeek 内网直连 430KB body 正常（HTTP 200 in=0.5s），说明协议转换、请求体构建均无问题。

2. **502 来自代理，不是 NVIDIA**：`in=0.0s` 表示代理在接受 CONNECT 后、转发请求前/时立即返回了 502（0B body）。NVIDIA 直连超时是正常的（本机无法直连外网）。

3. **curl 小请求通但极慢（30s）**：代理到 NVIDIA 的链路是通的，但有严重的延迟问题。

### 推测代理侧的问题

| 推测 | 证据 | 可能性 |
|------|------|--------|
| **大请求体被拒绝/触发限制** | curl 100B 通，holoProxy 400KB 瞬间 502 | ⭐⭐⭐⭐⭐ |
| **代理后端连接池耗尽** | 6 ESTABLISHED + 5 TIME_WAIT，可能达到上限 | ⭐⭐⭐⭐ |
| **代理对 HTTP/1.1 CONNECT 隧道处理有 bug** | reqwest `http1_only()` 与 curl 的行为差异 | ⭐⭐⭐ |
| **代理上游连接超时设置过短** | 502 0.0s + curl 30s 延迟 → 代理可能仅等了几秒就放弃 | ⭐⭐⭐ |
| **mitmproxy TLS 证书或 ALPN 协商问题** | reqwest 的 TLS 库 vs curl 的 openssl | ⭐⭐ |

### 最可能的根因

**代理对大请求体有大小限制或缓冲超时**。curl 100B 小请求能过（尽管 30s），holoProxy 400KB+ 请求瞬间被 502，说明代理可能在接收/转发请求体时遇到问题——可能是 max_body_size 限制、buffer 溢出、或解析大 body 时的异常。

代理应记录自身日志来确认：收到 holoProxy 请求后是在哪个环节返回的 502——(a) 解析请求时 (b) 连接上游时 (c) 转发响应时。日志中的 `in=0.0s` 强烈指向 (a) 或 (b)：代理根本没去连 NVIDIA 就返回了 502。

## 建议代理改进方向

### 高优先级
1. **增加请求体大小限制** — 检查是否有 `max_body_size` / `MAX_CONTENT_LENGTH` 配置，至少应支持 1MB
2. **增加上游连接超时** — NVIDIA API 的 TTFB 可能需要 30-60s，当前可能设置的过短
3. **添加连接池上限配置** — 当前 6+ 并发连接可能导致新请求被拒，建议至少支持 20+
4. **代理自身日志** — 记录每次 502 的原因：是上游不可达、超时、还是请求被拒绝

### 中优先级
5. **对 reqwest CONNECT 代理模式的兼容性测试** — reqwest 使用 HTTP/1.1 CONNECT 隧道 + `danger_accept_invalid_certs` 的组合可能与 mitmproxy 类的代理有差异
6. **请求体流式转发** — 不要等收完整个请求体再转发上游，边收边发以减少延迟

### 验证方法
在代理侧加入日志后，用以下方式复现：
```bash
# 模拟 holoProxy 的大请求体场景
curl -X POST --proxy http://127.0.0.1:60130 --proxy-insecure \
  https://integrate.api.nvidia.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d @<(python3 -c "
import json
msgs = [{'role':'user','content':'x'*500}] * 500
print(json.dumps({'model':'moonshotai/kimi-k3','messages':msgs,'stream':True,'max_tokens':100}))
  ") \
  --connect-timeout 10 --max-time 120
```
