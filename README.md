# 🚀 Proxy Bridge v2.0 🌉

**让所有 Windows 本地程序共享 Chrome 的网络能力！** — 高性能 HTTP/HTTPS 代理，通过 Chrome 扩展（ghelper）实现翻墙。

```
任意本地程序 → 0.0.0.0:60130 → Python 代理 → Chrome Native Messaging → ghelper → Chrome fetch() → 互联网
```

## 📥 一键安装

1. **下载 [ProxyBridge-Setup.exe](https://github.com/code-prometheus/proxy-bridge/releases/latest)**（右键 → 以管理员身份运行）
2. **重启 Chrome** — 扩展自动加载 + 代理自动启动
3. **Done!** `http://127.0.0.1:60130` 已就绪（远程访问 `http://<你的IP>:60130`）

```bash
# 验证
curl -x http://127.0.0.1:60130 https://www.google.com
curl -x http://127.0.0.1:60130 https://platform.worldquantbrain.com/sign-in
```

Setup.exe 自动完成：CA 证书 → CRX 打包 → Extension ID 计算 → NM 注册表 → Chrome 策略强制安装。

## 🧱 项目结构

| 文件 | 职责 |
|------|------|
| `AutoSetup.py` | 一键安装脚本（可编译为 exe） |
| `entry.py` | 主入口：CA 管理 + 启动代理 + NM bridge |
| `local_proxy.py` | 代理核心：MITM TLS 终止 + HTTP 转发 |
| `utils.py` | CertManager（CA + per-host 证书）、NM 队列、HTTP 解析 |
| `extension/` | Chrome MV3 扩展（background.js 处理 NM 消息 + fetch） |
| `chrome-native-config/` | NM 配置（run-host.bat、extension-key.pem） |

## 🔒 SSL 信任

- **CA Root**: `~/.proxy-bridge-ca/ca-cert.pem`（RSA 2048, 10 年）
- **Per-host cert**: `~/.proxy-bridge-ca/certs/{host}.crt`（CA 签名，SAN + EKU）
- **系统信任**: certutil 安装到 Windows Root Store

## 🔧 开发

```bash
pip install cryptography
python entry.py --init-ca
python entry.py --install-ca     # 管理员
python entry.py                   # 启动代理（需 Chrome 扩展已加载）
```

*Powered by Chrome Network Stack — 让代理无处不在*
