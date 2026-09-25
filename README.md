# Proxy Bridge v1.0.0

**让所有本地程序共享 Chrome 的网络能力** — 一个零配置的 HTTP/HTTPS 代理，通过 Chrome Native Messaging 实现上游转发。

```
任意程序 → 127.0.0.1:60130 (代理) → Chrome NM → Chrome fetch() → 互联网
```

## 核心特性

- **MITM TLS 终止** — 代理自签 per-host 证书，客户端只需信任本地 CA 一次
- **Chrome fetch() 唯一上游** — 所有请求经 Chrome 网络栈发出，共享 Chrome 的 cookies、HTTP/2、QUIC
- **流式传输** — body 边收边发，不缓冲到内存，支持任意大小文件
- **模块化 v1.0 架构** — `proxy.py` / `nm.py` / `upstream.py` / `certs.py` / `http_parser.py` / `connection.py`
- **Chrome 关闭自动释放端口** — NM 断开 → Python 进程退出 → 60130 端口释放
- **一键安装** — `ProxyBridge-Setup.exe` 管理员运行，自动配置 CA + NM 注册表

## 快速开始

### 安装

1. 下载 `ProxyBridge-Setup.exe`，右键 → **以管理员身份运行**
2. 选择安装目录（默认 `%USERPROFILE%\proxy-bridge`）
3. 打开 `chrome://extensions`，开启 **开发者模式**
4. 点击 **加载已解压的扩展** → 选择 `<安装目录>\extension`
5. 重启 Chrome — 代理自动启动

### 验证

```bash
# HTTP 代理
curl -x http://127.0.0.1:60130 http://httpbin.org/get

# HTTPS MITM（需要先 trust CA）
curl --cacert ~/.proxy-bridge-ca/ca-cert.pem -x http://127.0.0.1:60130 https://httpbin.org/anything
```

### Linux 客户端信任 CA

```bash
sudo cp ca-cert.pem /usr/local/share/ca-certificates/proxy-bridge.crt
sudo update-ca-certificates
```

## 架构

| 文件 | 职责 |
|------|------|
| `entry.py` | 入口：CA 管理 (`--init-ca`/`--install-ca`)、启动 proxy + NM bridge |
| `proxy.py` | 代理核心：MITM TLS 终止、HTTP/CONNECT 处理、连接池管理 |
| `nm.py` | Chrome NM 传输层：stdin/stdout length-prefixed JSON 通信 |
| `upstream.py` | 上游转发：队列流式传输 NM chunks → 客户端 |
| `certs.py` | 证书管理：根 CA 生成 + per-host 动态签发 |
| `http_parser.py` | HTTP/1.1 解析：header parse、chunked body、Content-Length body |
| `connection.py` | 连接抽象：统一管理 raw/TLS socket 生命周期 |
| `utils.py` | 配置加载、日志初始化 |
| `AutoSetup.py` | Windows 一键安装脚本 |
| `extension/` | Chrome MV3 扩展 (Service Worker + NM) |

## 开发

```bash
# 生成 CA
python entry.py --init-ca

# 安装 CA 到系统
python entry.py --install-ca   # Windows: 管理员权限

# 启动代理
python entry.py

# 运行测试
python -m pytest tests/ -v
```

## 许可

MIT
