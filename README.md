# **🌉 Super Proxy Bridge (混合双擎超级代理)**

**一个极简、硬核的 L4/L7 全栈网络穿透与代理桥接系统。** 本项目不仅能为量化交易、穿透内网提供**底层 RC4 加密的高并发多路复用隧道**，还能利用 Chrome Native Messaging 技术将流量**无缝回环至您的 Chrome 浏览器**。这意味着您可以直接利用浏览器的高级特征（完整的 TLS 指纹、Cookie）以及现有的代理插件（如 Ghelper、SwitchyOmega），彻底绕过高强度 WAF 与证书封锁。

## **🤔 痛点与终极解决方案**

在复杂的网络环境中（如服务器出海、机房 IP 被严格风控、本地 CLI 工具无法复用浏览器代理），开发者通常面临两个极端的挑战：

1. **传输层 (L4) 性能瓶颈**：量化交易（如 Pytdx 行情接口）或数据库连接需要极低的延迟和高并发，传统的“一请求一隧道”模式会耗尽 Socket 资源，引发拥塞死锁。  
2. **应用层 (L7) 特征被墙**：普通的 HTTP/HTTPS 代理极易被防火墙深度包检测（DPI）识别拦截；同时，命令行工具（如 pip, git, curl）经常遇到烦人的 SSL: CERTIFICATE\_VERIFY\_FAILED 证书报错，且无法共享浏览器里已经配置好的优质代理线路。

**Super Proxy Bridge 实现了“大一统”！** 我们抛弃了繁杂的 Node.js 依赖，**仅用纯 Python 脚本构建了混合双核引擎**：

* 🚄 **L4 隧道引擎**：一条经过 RC4 流密码全局混淆的长连接 TCP 隧道，内置**单连接多路复用 (Multiplexing)** 技术。1000 个高频请求也只占用 1 条底层 TCP 连接，拒绝拥塞。  
* 🕸️ **L7 桥接引擎**：内置动态 HTTPS MITM（中间人）证书签发引擎。结合 Chrome Native Messaging，将 HTTP(S) 流量无缝送入浏览器内部。**无论你在机房还是终端，发出的请求看起来就像是你电脑上的 Chrome 正在正常上网！**

## **🛠️ 混合架构工作原理**

【场景 1：本地 CLI 工具全能上网】  
\[Git / Pip / Curl\] \-\> (HTTP/HTTPS) \-\> \[Python 本地 60130 端口\]   
                                            | (动态 MITM 劫持解密)  
                                            V  
                                      \[Chrome 扩展\] \-\> \[Ghelper 等代理\] \-\> \[全球互联网\]

【场景 2：远端服务器/机房穿透出海】  
\[Pytdx 等应用\] \-\> \[Ubuntu 隧道服务端 8899\]  
                      | (多路复用 \+ RC4 流密码白噪声混淆)  
                      V  
\[互联网/严格防火墙\] \-\> \[Windows 客户端 6974\]   
                      |  
                      |--- (智能嗅探为 SOCKS5) \---\> \[原生 Socket 极速直连目标服务器\]  
                      |  
                      |--- (智能嗅探为 HTTP/HTTPS) \-\> ♻️ 触发 Loopback 回环路由！  
                                                      | \-\> \[送入本地 60130 端口\] \-\> 走 Chrome 浏览器出海！

## **✨ 核心特性**

* 🚀 **极简自动化部署**：提供 AutoSetup.bat，一键自动安装依赖、生成 CA 根证书、注入 Windows 系统信任库、配置全局环境变量。  
* 🛡️ **双重防风控体系**：底层 RC4 流加密让流量表现为高熵白噪声；上层则直接复用 Chrome 的真实 TLS 指纹与完整环境，防封锁能力拉满。  
* ⚡ **真正的多路复用**：自研轻量级多路复用协议，从根本上解决 TCP 握手开销与粘包问题。  
* 🔀 **智能协议嗅探**：服务端根据数据流首字节，自动区分 SOCKS5、HTTP 还是 HTTPS，实现智能分流。  
* 🚫 **无痛证书信任**：底层封装全局无条件信任逻辑，一键彻底解决各类脚本抓取或 CLI 工具的 SEC\_E\_UNTRUSTED\_ROOT 证书报错问题。

## **📦 快速部署指南**

### **第一部分：服务端部署 (机房/内网 Ubuntu 节点)**

1. 将项目中的 tunnel\_server\_and\_local\_proxy.py 上传至您的 Ubuntu 服务器。  
2. 在同目录下新建 config.ini，配置如下：  
   \[common\]  
   secret\_key \= 您的超强自定义密码\_必须与客户端保持一致

   \[server\]  
   tunnel\_bind\_ip \= 0.0.0.0  
   tunnel\_bind\_port \= 6974  
   proxy\_bind\_ip \= 127.0.0.1  
   proxy\_bind\_port \= 8899

3. 运行服务端引擎：  
   python3 tunnel\_server\_and\_local\_proxy.py

4. **完成！** 现在，Ubuntu 本地的应用只需设置代理为 SOCKS5/HTTP 127.0.0.1:8899，流量即可被加密打包发往您的 Windows 节点。

### **第二部分：客户端部署 (本地 Windows 出网节点)**

**前置要求**：请确保您的电脑已安装 **Python 3.8+** 以及 **Google Chrome** 浏览器。

1. **加载 Chrome 扩展**：  
   * 打开 Chrome 浏览器，访问 chrome://extensions/。  
   * 开启右上角的 **开发者模式**。  
   * 点击“加载已解压的扩展程序”，选择本项目的 extension 文件夹。  
   * 复制生成的扩展卡片上的 **ID**（一串 32 位的字母）。  
2. **一键安装底层核心**：  
   * 在项目根目录，右键点击 AutoSetup.bat，选择 **“以管理员身份运行”**。  
   * 按照终端提示，粘贴刚刚复制的 Chrome 扩展 ID。  
   * *脚本将自动安装 cryptography 库，为您生成专属 CA 证书并注入系统。*  
3. **连接打通**：  
   * **彻底关闭并重新打开 Chrome 浏览器**。  
   * 点击浏览器右上角的 Proxy Bridge 扩展图标。如果显示 🟢 **绿灯**，并提示连接成功，则一切就绪！*(客户端引擎已完全接管，无需再手动修改代理配置)*

## **💻 常用 CLI 工具无痛代理指南 (L7 引擎)**

安装完 Windows 客户端后，您的本地机器 127.0.0.1:60130 已经化身为一个全能的 HTTP/HTTPS 代理网关，完美继承了 Chrome 的网络环境！

### **1\. Python Pip 极速同步**

得益于 AutoSetup.bat 自动注入的 PIP\_CERT 环境变量，您可以直接无视证书错误：

pip install django \--proxy \[http://127.0.0.1:60130\](http://127.0.0.1:60130)

*(💡 推荐将其写入 pip.ini 实现全局自动代理)*

### **2\. Git 无缝加速**

Git For Windows 默认使用 Schannel 校验，只需执行以下命令切换至 OpenSSL 并信任我们的本地 CA 即可：

git config \--global http.proxy \[http://127.0.0.1:60130\](http://127.0.0.1:60130)  
git config \--global https.proxy \[http://127.0.0.1:60130\](http://127.0.0.1:60130)  
git config \--global http.sslBackend openssl  
git config \--global http.sslCAInfo "%USERPROFILE%/.proxy-bridge-ca/ca-cert.pem"

### **3\. Curl / NPM 等通用工具**

部署脚本已自动在后台为您写入了 %USERPROFILE%\\\_curlrc 和 NODE\_EXTRA\_CA\_CERTS，天生免疫证书报错。只需在终端设置环境变量：

set HTTP\_PROXY=\[http://127.0.0.1:60130\](http://127.0.0.1:60130)  
set HTTPS\_PROXY=\[http://127.0.0.1:60130\](http://127.0.0.1:60130)  
curl \[https://www.google.com\](https://www.google.com)

## **⚠️ 安全与隐私声明**

* **100% 本地运行**：本项目所有的 MITM 证书动态签发、RC4 流量加解密均在您的**本地计算机和您的私人服务器**上完成，绝不经过任何第三方节点。  
* **妥善保管私钥**：系统生成的根证书（CA）保存在 %USERPROFILE%\\.proxy-bridge-ca\\ 目录下。**请务必妥善保管私钥文件 (ca-key.pem)**，切勿上传至公共网络。

## **🤝 贡献与支持**

如果这个项目帮助您突破了网络封锁、极大地提升了量化交易的稳定性，欢迎给项目点个 ⭐️ **Star**！您的支持是我们持续优化的最大动力。

欢迎提交 Issue 反馈问题，或提交 Pull Request 共建社区。

## **📄 License**

本项目基于 [MIT License](http://docs.google.com/LICENSE) 开源。