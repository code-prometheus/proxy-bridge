# Proxy Bridge CLAUDE.md

## 项目概述

Proxy Bridge 是一个本地 MITM 代理 + RC4 加密隧道 + LLM API 桥接的综合性网络工具。

## 核心架构

- `tunnel_client_and_remote_proxy.py` — 主入口：启动本地代理 + 隧道客户端 + Chrome Native Bridge
- `local_proxy.py` — 本地 MITM 代理（HTTP/HTTPS）+ LLM API 路由
- `remote_tunnel.py` — RC4 加密隧道客户端
- `tunnel_server_and_local_proxy.py` — 远程服务器端：隧道 + SOCKS5/HTTP 代理
- `utils.py` — 共享工具库（CertManager, RC4, 配置管理, HTTP 解析）
- `extension/` — Chrome Manifest V3 扩展
- `AutoSetup.bat` — Windows 一键部署

## 关键技术要点

- 单端口 `127.0.0.1:60130` 同时服务代理转发和 API 请求
- MITM HTTPS 使用自签 CA 证书（`~/.proxy-bridge-ca/`）
- 隧道使用 RC4 流加密
- Chrome Native Messaging 实现浏览器与 Python 进程通信
- 域名智能分流：直连失败自动学习加入代理名单

## 依赖

- Python 3.8+
- `cryptography` 库（唯一核心依赖）

## 命令

- 初始化 CA: `python tunnel_client_and_remote_proxy.py --init-ca`
- 启动服务: `python tunnel_client_and_remote_proxy.py`
- 远程服务器: `python tunnel_server_and_local_proxy.py`
- 一键部署: `./AutoSetup.bat`（需管理员权限）
