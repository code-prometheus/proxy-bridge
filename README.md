# 🌉 Proxy Bridge

**让本地 CLI 工具无缝复用 Ghelper 等代理插件，通过代理让所有应用都可以科学上网**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Node.js](https://img.shields.io/badge/Node.js-v18+-green.svg)](https://nodejs.org/)
[![Chrome](https://img.shields.io/badge/Chrome-Extension-blue.svg)](https://developer.chrome.com/docs/extensions/)
[![Windows](https://img.shields.io/badge/OS-Windows-blue.svg)](https://www.microsoft.com/windows)

## 🤔 痛点与解决方案

作为开发者，你是否经常遇到以下“网络墙”与“证书墙”问题？
- 使用 `git clone` 或 `pip install` 时网络卡死，被迫使用国内镜像源，却经常遇到版本不同步、依赖缺失或同步延迟。
- 本地配置了代理软件，但命令行工具（CLI）不认代理，或者遇到烦人的 `SSL: CERTIFICATE_VERIFY_FAILED` / `SEC_E_UNTRUSTED_ROOT` 证书报错。
- 某些内部系统或特定网站必须依赖 Chrome 的登录态（Cookie）或特定的浏览器代理插件（如 **Ghelper**）才能访问，CLI 工具无能为力。

**Proxy Bridge** 完美解决了这些问题！它通过 Chrome Native Messaging 技术，在你的本地建立一个 **HTTPS MITM（中间人）代理桥梁**。它可以将本地 CLI 工具的流量无缝转发给 Chrome 浏览器，**直接复用 Chrome 的网络通道、Cookie 以及 Ghelper 等代理插件的优质线路**。只要 Chrome 能上的网，你的命令行就能上！

## ✨ 核心特性

- 🚀 **一键部署**：提供 `AutoSetup.bat`，自动处理 Node 依赖、系统证书信任、环境变量注入和注册表配置，小白也能 30 秒搞定。
- 🔒 **HTTPS 动态 MITM**：基于 `node-forge` 动态签发本地 CA 证书，完美拦截并转发 HTTPS 流量，CLI 工具无感知。
- 🌐 **复用浏览器生态**：完美配合 Ghelper 等 Chrome 代理插件，无需在终端重复配置复杂的代理账号密码，让所有应用轻松科学上网。
- 🛡️ **系统级信任**：自动将本地 CA 注入 Windows 信任库，配置全局环境变量，并优雅绕过 Schannel 的 CRL 吊销检查，彻底告别证书报错。
- 🔄 **协议兼容**：完美支持 HTTP/HTTPS CONNECT 隧道，兼容几乎所有支持代理的命令行工具。

## 🛠️ 工作原理

    [本地 CLI 工具] (git / pip / npm / curl / wget)
           │ (HTTP/HTTPS 请求)
           ▼
    [Node.js 本地代理] (127.0.0.1:60130) ──(动态 MITM 签发证书)──> [系统信任 CA]
           │ (Chrome Native Messaging 管道)
           ▼
    [Chrome 扩展] (background.js fetch API)
           │ (复用浏览器网络与 Cookie)
           ▼
    [Chrome 代理插件] (如 Ghelper) ──> [全球互联网 / GitHub / PyPI 官方源]

---

## 📦 快速开始

### 前置要求
1. **Node.js** (v18 或更高版本，推荐 LTS)
2. **Google Chrome** 浏览器
3. **Chrome 代理插件** (如 Ghelper，并已配置好可用线路)

### 安装步骤

#### 1. 加载 Chrome 扩展
- 打开 Chrome，访问 `chrome://extensions/`
- 开启右上角的 **开发者模式**
- 点击 **加载已解压的扩展程序**，选择本项目的 `extension` 文件夹。
- **重要**：在扩展卡片上找到并复制该扩展的 **ID**（一串 32 位的字母，例如 `abcdefghijklmnopqrstuvwxyzabcdef`）。

#### 2. 一键部署本地宿主
- 找到项目根目录下的 `AutoSetup.bat`。
- **右键 -> 以管理员身份运行**（必须，因为需要写入系统证书和注册表）。
- 按照终端提示，粘贴你刚才复制的 **扩展 ID**。
- 脚本会自动完成：依赖安装、CA 证书生成、系统信任库注入、环境变量配置、`_curlrc` 生成以及 Native Messaging 注册。

#### 3. 激活连接
- **彻底关闭并重启 Chrome**（确保 Native Messaging 管道重新初始化）。
- 点击 Chrome 右上角的 Proxy Bridge 扩展图标，看到 🟢 **绿灯** 即表示连接成功！

---

## 💻 常用 CLI 工具全局配置指南

现在，你的本地代理已经就绪，地址为 `http://127.0.0.1:60130`。
*注意：请确保你的 Chrome 处于打开状态，且 Ghelper 等代理插件已启用并处于全局或规则代理模式。*

### 1. Git 加速 (直连 GitHub 官方)

由于 Windows 下的 Git 默认使用 Schannel 作为 SSL 后端，直接配置代理可能会遇到 `SEC_E_UNTRUSTED_ROOT` 证书报错。我们需要让 Git 使用 OpenSSL 并信任我们的本地 CA 证书。

**开启全局代理与证书信任 (在 CMD 中运行)：**

    :: 1. 设置代理指向 Proxy Bridge
    git config --global http.proxy http://127.0.0.1:60130
    git config --global https.proxy http://127.0.0.1:60130
    
    :: 2. 切换 SSL 后端为 OpenSSL 并指定本地 CA 证书 (解决证书报错)
    git config --global http.sslBackend openssl
    git config --global http.sslCAInfo "%USERPROFILE%/.proxy-bridge-ca/ca-cert.pem"

**使用示例：**

    git clone https://github.com/torvalds/linux.git

**关闭 Git 代理 (恢复直连)：**

    git config --global --unset http.proxy
    git config --global --unset https.proxy

### 2. Python Pip 同步 (直连 PyPI 官方)

Python 的 pip 默认使用内置的 `certifi` 证书包，**既不读取 Windows 系统信任库，也不读取 Linux 的 `/etc/ssl/certs`**。因此，在开启代理时极易出现 `SSL: CERTIFICATE_VERIFY_FAILED`。

**Windows 用户 (永久信任)：**
在 CMD 中运行一次（需重启 CMD 生效）：
    setx PIP_CERT "%USERPROFILE%\.proxy-bridge-ca\ca-cert.pem"

**Linux / macOS 用户 (降维打击：注入 certifi)：**
由于 pip 底层只认 `certifi` 包，最彻底的方法是将本地 CA 追加到 pip 的证书库末尾。在终端运行：
    # 获取 pip 证书库路径
    CERT_PATH=$(python3 -m certifi)
    # 将本地 CA 强行追加到证书库末尾 (系统级 Python 需加 sudo)
    sudo sh -c "cat ~/.proxy-bridge-ca/ca-cert.pem >> $CERT_PATH"

**配置全局代理与白名单 (推荐所有平台)：**
为了省去每次敲 `--proxy` 的麻烦，并彻底规避 CDN 重定向导致的证书校验 Bug，建议配置 `pip.conf` / `pip.ini`。
在 Linux/macOS 下运行：
    mkdir -p ~/.config/pip
    cat <<EOF > ~/.config/pip/pip.conf
    [global]
    proxy = http://127.0.0.1:60130
    trusted-host =
        pypi.org
        files.pythonhosted.org
        github.com
    EOF

在 Windows 下，在 `%APPDATA%\pip\pip.ini` 中写入相同内容（注意 Windows 路径使用正斜杠 `/`）。

**使用示例：**
配置完成后，直接无脑安装，自动走 Chrome 代理通道！
    pip install django

### 3. NPM / Yarn 依赖下载 (Node.js 生态)

Node.js 会自动读取 `AutoSetup.bat` 注入的 `NODE_EXTRA_CA_CERTS` 环境变量，因此天生免疫证书报错，只需配置代理即可。

**开启全局代理：**

    npm config set proxy http://127.0.0.1:60130
    npm config set https-proxy http://127.0.0.1:60130

**使用示例：**

    npm install express

**关闭 NPM 代理 (恢复直连)：**

    npm config rm proxy
    npm config rm https-proxy

### 4. 通用终端环境变量 (适用于 wget, curl, go get 等)

如果你使用的工具支持标准的 HTTP 代理环境变量，只需在当前终端窗口设置即可（`AutoSetup.bat` 已自动为你配置好了 curl 的 `_curlrc`，所以 curl 可以直接使用）。

**Windows CMD:**

    set HTTP_PROXY=http://127.0.0.1:60130
    set HTTPS_PROXY=http://127.0.0.1:60130

**Windows PowerShell:**

    $env:HTTP_PROXY="http://127.0.0.1:60130"
    $env:HTTPS_PROXY="http://127.0.0.1:60130"

**Linux / macOS (Bash/Zsh):**

    export http_proxy=http://127.0.0.1:60130
    export https_proxy=http://127.0.0.1:60130

---

## 🧠 进阶：Windows 证书信任机制解析 (硬核)

很多开发者在 Windows 下使用 MITM 代理时会遇到 `curl: (60) schannel: SEC_E_UNTRUSTED_ROOT` 或 `CERT_TRUST_REVOCATION_STATUS_UNKNOWN`。本项目在 `AutoSetup.bat` 中通过以下机制彻底解决了这一业界难题：

1. **双库注入**：使用 PowerShell 将 CA 证书同时注入 `Cert:\LocalMachine\Root` 和 `Cert:\CurrentUser\Root`。
2. **环境变量兜底**：注入 `CURL_CA_BUNDLE`、`SSL_CERT_FILE`、`PIP_CERT` 等全局环境变量，让 Python/Node.js 等非 Schannel 后端的工具自动信任证书。
3. **绕过 Schannel CRL 检查**：Windows 的 Schannel（curl 默认后端）会强制运行 CRL（证书吊销列表）检查。由于本地生成的 CA 没有真实的 CRL 服务器，会导致 `STATUS_UNKNOWN` 报错。本脚本通过在 `%USERPROFILE%\_curlrc` 中自动配置 `ssl-no-revoke`，并修改注册表 `CertificateRevocation=0`，优雅地绕过了这一限制，实现了真正的“无感信任”。
4. **Git OpenSSL 切换**：针对 Git for Windows，通过 `http.sslBackend openssl` 绕过 Schannel 的严格限制，直接读取本地 PEM 证书文件，实现完美握手。

---

## ⚠️ 安全与隐私声明

- **本地运行**：所有的 MITM 证书签发和流量转发均在你的**本地机器**上完成，不经过任何第三方服务器。
- **证书安全**：根证书（CA）保存在 `%USERPROFILE%\.proxy-bridge-ca\` 目录下。**请妥善保管您的私钥（ca-key.pem），切勿将其分享给他人或上传至公共网络**，否则他人可伪造您的 HTTPS 流量。
- **卸载**：如需卸载，只需删除项目文件夹，并在 `chrome://extensions/` 中移除扩展。系统证书可通过 Windows 证书管理器（`certmgr.msc`）手动删除 "Proxy Bridge Local CA"。

---

## ❓ 常见问题 (FAQ)

**Q: 为什么 Chrome 扩展图标是红灯/灰灯？**
A: 请确保 Node.js 已正确安装，且 `AutoSetup.bat` 以**管理员身份**运行成功。尝试彻底关闭 Chrome 后重新打开，并点击扩展图标重试。

**Q: 遇到 `SSL: CERTIFICATE_VERIFY_FAILED` 怎么办？**
A: 请尝试**重启电脑**以刷新 Windows Schannel 的证书缓存，或重新以管理员身份运行一次 `AutoSetup.bat` 以修复环境变量。对于 Git，请确保执行了 README 中的 `http.sslBackend openssl` 配置；对于 Pip，请确保执行了 `setx PIP_CERT`。

**Q: 支持 macOS 或 Linux 吗？**
A: 目前 `AutoSetup.bat` 专为 Windows 环境深度优化（处理了复杂的注册表和 Schannel 证书信任）。核心 Node.js 代码是跨平台的，欢迎社区大佬提交 macOS/Linux 的 Shell 部署脚本 PR！

---

## 🤝 贡献与支持

如果这个项目帮你节省了配置网络环境的时间，让你成功拉取了急需的依赖包，实现了全终端的科学上网，**请给这个项目一个 ⭐️ Star 吧！** 你的支持是我持续优化和开源的最大动力！

欢迎提交 Issue 反馈问题，或提交 PR 完善功能。

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。