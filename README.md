# **🚀 Proxy Bridge 🌉**

**让本地代理与 AI 能力无缝连接！** \> Proxy Bridge 是一个极致精简、高性能的代理桥接与 LLM 接口透传解决方案。

## **💡 为什么选择 Proxy Bridge？**

在开发与生产环境中，您是否也曾被网络环境困扰？Proxy Bridge 帮您一键解决：

* **⚡ 单端口复用**: 只需要 60130 一个端口，同时搞定代理转发与 API 服务。  
* **🤖 AI 亲和**: 内置 Claude Code 标准 Anthropic 接口转换，让您的工具轻松调用本地/远程 LLM。  
* **🌍 智能科学上网**: 配合 Chrome 扩展，流量自动分流，办公学习两不误。  
* **🔗 极致透传**: 远程服务器通过隧道直接接管本地网络，人在天涯，如在身旁。

## **🛠 核心架构图**

\[浏览器/客户端\]   
      │   
      ▼ (请求)  
\[ 127.0.0.1:60130 \]   
      ├─── \[ TCP 转发 \] ──▶ 公网服务  
      ├─── \[ HTTP/HTTPS \] ──▶ Chrome 代理 (科学上网)  
      └─── \[ /v1/messages \] ──▶ LLM 接口转换 (Claude Code 支持)

## **🚀 核心功能亮点**

| 功能 | 描述 |
| :---- | :---- |
| **🌐 智能网桥** | TCP 直连 \+ HTTP/HTTPS 浏览器分流。 |
| **🧠 API 适配器** | 将一切 LLM 请求转化为 Claude Code 兼容的 Anthropic 协议。 |
| **🛡 反向隧道** | 将远程主机作为您的私人代理中转站。 |
| **🛠 一键部署** | 支持 Windows 自动化安装，开箱即用。 |

## **📦 快速起步**

### **1\. 自动化安装**

Windows 用户无需繁琐配置，直接双击运行：

./AutoSetup.bat

### **2\. 配置说明**

* **Chrome 代理**: 导入 chrome-native-config 目录下的 JSON 配置，让浏览器即刻变身超级入口。  
* **参数微调**: 编辑 settings.json，根据您的代理需求进行定制化设置。

### **3\. 远程隧道**

在您的远程云服务器上运行 tunnel\_server\_and\_local\_proxy.py，即可开启无界办公：

python tunnel\_server\_and\_local\_proxy.py

## **🤝 期待您的支持！**

如果您觉得这个工具对您有帮助，请不要吝啬您的 **Star ⭐**，您的鼓励是我们持续维护的动力！

* 💡 **反馈建议**: 欢迎提交 [Issue](https://github.com/your-repo/proxy-bridge/issues)。  
* 🚀 **参与贡献**: 欢迎 Fork 本项目并提交 PR。

*Powered by Proxy Bridge \- 构建本地与远程的高效桥梁*