# CLAUDE.md

本文件为Claude Code (claude.ai/code)提供在此代码库工作的指南。

## 项目概述

Proxy Bridge是一个先进的网络解决方案，支持在受限环境与外部服务之间建立安全通信。系统包含三个核心组件：

1. **本地代理** (`local_proxy.py` / `tunnel_server_and_local_proxy.py`) - 运行在防火墙后的客户端机器上
2. **隧道层** - 加密的WebSocket通信通道
3. **远程代理** (`remote_tunnel.py` / `tunnel_client_and_remote_proxy.py`) - 运行在可访问互联网的服务器上

## 核心组件

- **代理逻辑核心**:
  - `claude_anthropic_proxy.py`: 直接互联网访问的独立HTTP代理
  - `local_proxy.py`: 本地端代理实现
  - `remote_tunnel.py`: 远程端代理实现

- **隧道基础设施**:
  - `tunnel_server_and_local_proxy.py`: 本地代理+隧道服务器的组合
  - `tunnel_client_and_remote_proxy.py`: 远程代理+隧道客户端的组合

- **支持模块**:
  - `utils.py`: 共享工具（加密、配置解析、证书管理）
  - `settings.json`: 配置文件

- **浏览器集成**:
  - `extension/`: 用于UI和原生消息传递的Chrome扩展
  - `chrome-native-config/`: 原生消息主机配置

## 系统架构

运行流程：


客户端应用 → 本地代理 → WebSocket隧道 → 远程代理 → 互联网


核心特性：
- 隧道流量使用RC4加密
- HTTPS拦截的MITM证书处理
- 协议感知流量路由（HTTP/SOCKS）
- 通过Chrome扩展进行配置

## 开发命令

### 运行组件

**本地端 (Windows):**
bash
python tunnel_server_and_local_proxy.py


**远程端 (Ubuntu):**
bash
python tunnel_client_and_remote_proxy.py


**独立代理:**
bash
python claude_anthropic_proxy.py


**自动安装 (Windows):**
bash
AutoSetup.bat


### Chrome扩展开发
1. 通过`chrome://extensions`加载`extension/`目录中的扩展
2. 原生消息配置在`chrome-native-config/`中

### 配置管理
- 主配置: `settings.json`
- CA证书存储在`~/.proxy-bridge-ca/`

## 安全注意事项

- **密钥管理**: `settings.json`中的`SECRET_KEY`必须保护
- **证书处理**: CA私钥存储在`~/.proxy-bridge-ca/ca-key.pem`
- **输入验证**: 严格的协议解析以防止注入攻击

## 重要说明

1. Chrome扩展(`extension/`)通过原生消息通信
2. `AutoSetup.bat`处理依赖安装和证书配置
3. 系统使用双重RC4密钥进行双向隧道加密
4. 所有组件为Python 3.8+设计，依赖最小化
5. 无需构建过程 - 纯Python实现