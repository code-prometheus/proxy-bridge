# 🚀 Proxy Bridge v2.0 🌉

**让所有 Windows 本地程序共享 Chrome 的网络能力！** — 一个高性能 HTTP/HTTPS 代理，通过 Chrome 扩展（ghelper）实现翻墙。

```
任意本地程序 → 127.0.0.1:60130 → Python 代理 → Chrome Native Messaging → ghelper 扩展 → Chrome fetch() → 互联网
```

## 💡 为什么选择 Proxy Bridge？

- **⚡ 全能代理**: 60130 端口提供完整 HTTP/HTTPS 代理，支持 GET/POST/PUT/DELETE/CONNECT 所有方法
- **🔐 MITM HTTPS**: 自动生成 CA 证书，支持 HTTPS 中间人解密，完整 Set-Cookie 支持
- **🧵 高性能**: ThreadPoolExecutor(500 workers)，每个请求独立线程，零阻塞
- **🎯 端口映射**: `Host: example.com:8888` 自动识别非标准端口
- **🛡 安全**: 仅监听 `127.0.0.1`，不接受外部连接

## 📦 快速起步

### 1. 安装依赖
```bash
pip install cryptography
```

### 2. 生成 CA 证书
```bash
python entry.py --init-ca
```

### 3. 安装 CA 证书到系统（以管理员身份运行）
```bash
python entry.py --install-ca
```

### 4. 加载 Chrome 扩展
1. 打开 Chrome，访问 `chrome://extensions/`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展」，选择 `extension/` 目录

### 5. 配置 Chrome Native Messaging
根据 `extension/` 目录下的扩展 ID，配置 Native Messaging 注册表项。

### 6. 启动代理
```bash
python entry.py
```

### 7. 配置应用程序使用代理
设置任意程序（curl、pip、git、IDE 等）的 HTTP/HTTPS 代理为：
```
http://127.0.0.1:60130
https://127.0.0.1:60130
```

## 📁 项目结构

| 文件 | 说明 |
| :---- | :---- |
| `entry.py` | 主入口：启动代理 + Chrome Native Bridge |
| `local_proxy.py` | HTTP/HTTPS 代理核心（MITM + 多线程） |
| `utils.py` | 工具库：证书管理、HTTP 解析、配置、NM 队列 |
| `extension/` | Chrome Manifest V3 扩展（网络引擎） |
| `settings.json` | 配置文件（首次运行自动生成） |

## 🔒 SSL 证书信任链

Proxy Bridge 使用自签 CA 证书实现 MITM HTTPS：
- **CA Root**: `~/.proxy-bridge-ca/ca-cert.pem`（RSA 2048, 10 年有效期）
- **主机证书**: `~/.proxy-bridge-ca/certs/{host}.crt`（动态生成，CA 签名）
- **证书扩展**: SAN（DNSName）、EKU（serverAuth）、SKI、AKI 完整信任链
- **系统集成**: `--install-ca` 通过 certutil 安装到 Windows 根证书存储区

## 🤝 期待您的支持！

如果您觉得这个工具对您有帮助，请给一个 **Star ⭐**！

- 💡 **反馈建议**: 欢迎提交 [Issue](https://github.com/code-prometheus/proxy-bridge/issues)
- 🚀 **参与贡献**: 欢迎 Fork 本项目并提交 PR

*Powered by Chrome Network Stack — 让代理无处不在*
