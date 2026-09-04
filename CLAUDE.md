# Proxy Bridge v2.0 CLAUDE.md

## 项目概述

Proxy Bridge 是一个本地 HTTP/HTTPS 代理服务器，利用 Chrome 扩展（ghelper）的网络栈实现翻墙。

**核心链路**: `任意本地程序 → 127.0.0.1:60130 → Python 代理 → Native Messaging → Chrome 扩展 → Chrome fetch() → 互联网`

## 核心架构

- `entry.py` — 主入口：启动代理服务器 + Chrome Native Bridge
- `local_proxy.py` — 本地 HTTP/HTTPS 代理核心（MITM）
- `utils.py` — 共享工具库（CertManager 证书管理、HTTP 解析、配置、NM 队列）
- `extension/` — Chrome Manifest V3 扩展（网络引擎）

## 关键技术要点

- 单端口 `127.0.0.1:60130` 同时服务 HTTP 和 HTTPS 代理
- MITM HTTPS 使用自签 CA 证书（`~/.proxy-bridge-ca/`）
- Chrome Native Messaging 实现浏览器与 Python 进程通信
- ThreadPoolExecutor(500 workers) 多线程并发，每连接独立线程
- 支持完整 HTTP 方法：GET、POST、PUT、DELETE、PATCH、HEAD、OPTIONS、CONNECT
- 支持端口映射：`Host: example.com:8888` 自动连接目标 8888 端口
- 支持多值 Set-Cookie 头（Chrome → Python 数组格式，Python 逐条输出）

## 依赖

- Python 3.8+
- `cryptography` 库（唯一核心依赖）

## 命令

- 初始化 CA: `python entry.py --init-ca`
- 安装 CA 到系统信任存储区: `python entry.py --install-ca`（需管理员权限）
- 启动服务: `python entry.py`
- Chrome 扩展加载：`extension/` 目录（开发者模式）
