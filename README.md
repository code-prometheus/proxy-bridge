# Proxy Bridge v2.0

**让所有本地程序共享 Chrome 的网络能力** — HTTP/HTTPS 代理，通过 Chrome 扩展实现。

```
任意程序 -> 127.0.0.1:60130 (代理) -> Chrome NM -> Chrome fetch() -> 互联网
```

## 安装

1. 下载 `ProxyBridge-Setup.exe`，右键 -> **以管理员身份运行**
2. 选择安装目录（默认 `%USERPROFILE%\proxy-bridge`）
3. 打开 `chrome://extensions`，开启 **开发者模式**
4. 点击 **加载已解压的扩展** -> 选择 `<安装目录>\extension`
5. 重启 Chrome — 代理自动启动

```bash
# 验证
curl -x http://127.0.0.1:60130 https://www.google.com
curl -x http://127.0.0.1:60130 https://platform.worldquantbrain.com/sign-in
```

代理监听 `0.0.0.0:60130`，局域网其他设备也能用。

## Linux 信任 CA

安装目录下有个 `ca-cert.pem`，复制到 Linux：

```bash
sudo cp ca-cert.pem /usr/local/share/ca-certificates/proxy-bridge-ca.crt
sudo update-ca-certificates
```

## 项目结构

| 文件 | 职责 |
|------|------|
| `AutoSetup.py` | 一键安装（exe 入口） |
| `entry.py` | 主入口：CA 管理 + 启动代理 + NM bridge |
| `local_proxy.py` | 代理核心：MITM TLS 终止 + HTTP 转发 |
| `utils.py` | CertManager（CA + per-host 证书）、NM 队列、HTTP 解析 |
| `extension/` | Chrome MV3 扩展 |
| `chrome-native-config/` | NM 配置 + 密钥（安装时生成） |

## 开发

```bash
pip install cryptography
python entry.py --init-ca
python entry.py --install-ca  # 管理员
python entry.py                # 启动（需 Chrome 扩展已加载）
```

## 打包

```bash
python -m PyInstaller ProxyBridge-Setup.spec --distpath dist --workpath build --clean
```
