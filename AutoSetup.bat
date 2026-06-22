@echo off
:: 强制使用 UTF-8 编码，彻底解决 Windows CMD 中文乱码问题
chcp 65001 >nul
setlocal

set "NATIVE_NAME=com.example.proxy_bridge"
set "ROOT_DIR=%~dp0"
set "NH_DIR=%ROOT_DIR%chrome-native-config"
set "CONFIG_FILE=%ROOT_DIR%settings.json"
set "CA_DIR=%USERPROFILE%\.proxy-bridge-ca"
set "CA_CERT=%CA_DIR%\ca-cert.pem"
set "PY_SCRIPT=%ROOT_DIR%tunnel_client_and_remote_proxy.py"

echo ==========================================
echo   Super Bridge: Python Dual-Engine Setup
echo ==========================================

:: 提权检查（保持单行 powershell 形式以防干扰）
net session >nul 2>&1
if not errorlevel 1 goto ADMIN_OK
powershell -Command "Start-Process -FilePath \"%~f0\" -Verb RunAs"
exit /b

:ADMIN_OK
echo [OK] Admin rights confirmed.

if exist "%PY_SCRIPT%" goto PY_SCRIPT_EXISTS
echo [ERROR] 找不到核心文件: tunnel_client_and_remote_proxy.py
echo 请确保该文件与本 bat 脚本放在同一目录下！
pause
exit /b 1

:PY_SCRIPT_EXISTS
if exist "%CONFIG_FILE%" goto CONFIG_FILE_EXISTS
echo [INFO] 尚未发现 settings.json
echo [INFO] 无需担心，稍后底层引擎将自动为您生成包含多模型的安全配置模板！

:CONFIG_FILE_EXISTS
echo [1/7] Checking Python Environment...
python --version >nul 2>&1
if not errorlevel 1 goto PYTHON_OK
echo [ERROR] Python not found in PATH! Please install Python 3.8+
pause
exit /b 1

:PYTHON_OK
echo [2/7] Nuking old processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM node.exe /T >nul 2>&1
timeout /t 2 >nul
echo [OK] Processes killed.

echo [3/7] Cleaning old JS environment files...
if exist "%ROOT_DIR%native-host" rd /s /q "%ROOT_DIR%native-host" >nul 2>&1
if exist "%NH_DIR%" rd /s /q "%NH_DIR%" >nul 2>&1
mkdir "%NH_DIR%"
REG DELETE "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /f >nul 2>&1
REG DELETE "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /f >nul 2>&1
echo [OK] System purged.

echo [4/7] Installing Python dependencies...
:: 1. 备份原始 Proxy 设置 (同时处理大小写敏感的代理环境变量)
set "ORIG_HTTP_PROXY=%http_proxy%"
set "ORIG_HTTPS_PROXY=%https_proxy%"
set "ORIG_HTTP_PROXY_UP=%HTTP_PROXY%"
set "ORIG_HTTPS_PROXY_UP=%HTTPS_PROXY%"

echo [INFO] 正在临时清空代理设置，以纯净直连模式尝试国内源...
:: 2. 彻底禁用当前会话的代理
set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="

:: 3. 尝试无代理情况下使用腾讯云镜像站安装
echo [INFO] 正在尝试通过腾讯云源直连安装...
python -m pip install cryptography headroom -i https://mirrors.cloud.tencent.com/pypi/simple/ --trusted-host mirrors.cloud.tencent.com >nul 2>&1
if not errorlevel 1 goto PIP_SUCCESS

:: 4. 尝试无代理情况下使用清华源安装
echo [WARNING] 腾讯云直连安装失败，正在尝试清华大学镜像源 (无代理模式)...
python -m pip install cryptography headroom -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn >nul 2>&1
if not errorlevel 1 goto PIP_SUCCESS

:: 5. 如果无代理国内源都失败了，恢复原始代理并使用官方 PyPI 源安装
echo [WARNING] 纯净无代理模式下的国内源均安装失败。
echo [INFO] 正在恢复您的原始代理设置，切换为使用代理连接官方 PyPI 源进行安装...
set "http_proxy=%ORIG_HTTP_PROXY%"
set "https_proxy=%ORIG_HTTPS_PROXY%"
set "HTTP_PROXY=%ORIG_HTTP_PROXY_UP%"
set "HTTPS_PROXY=%ORIG_HTTPS_PROXY_UP%"

python -m pip install cryptography headroom
if not errorlevel 1 goto PIP_SUCCESS

echo [ERROR] 依赖包安装失败！请在终端手动运行以下命令排查报错：
echo         python -m pip install cryptography headroom
pause
exit /b 1

:PIP_SUCCESS
echo [OK] Python dependencies ready.

echo [5/7] Generating Run Batch for Chrome Native Messaging...
:: 生成中转批处理，改写单行写入，防止嵌套
echo @echo off > "%NH_DIR%\run-host.bat"
echo cd /d "%%~dp0.." >> "%NH_DIR%\run-host.bat"
echo python "tunnel_client_and_remote_proxy.py" >> "%NH_DIR%\run-host.bat"

echo [6/7] Registering Native Messaging...
set "EXT_ID="
set /p EXT_ID=Paste Chrome Extension ID (请右键粘贴你复制的扩展 ID): 

set "BP=%NH_DIR%\run-host.bat"
set "BPE=%BP:\=\\%"
echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%BPE%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%NH_DIR%\%NATIVE_NAME%.json"
REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1

echo [7/7] Generating and Injecting CA to System...
:: 触发 Python 初始化命令。这一步不仅会生成 CA 证书，还会同时生成 settings.json！
python "%PY_SCRIPT%" --init-ca >nul 2>&1
timeout /t 3 >nul

if not exist "%CA_CERT%" goto CA_FAILED

:: 👇 核心修复：弃用容易静默失败的 powershell，改用 Windows 底层自带的 certutil 强制注入证书库！
echo [INFO] 正在将 Proxy Bridge CA 证书强制写入 Windows 根证书库...
certutil -addstore -f "Root" "%CA_CERT%" >nul 2>&1
certutil -user -addstore -f "Root" "%CA_CERT%" >nul 2>&1

setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
setx PIP_CERT "%CA_CERT%" >nul 2>&1

:: 👇 新增这一行：解决基于 Node.js 的工具（如 Claude Code, NPM 等）不信任本地 CA 的痛点！
setx NODE_EXTRA_CA_CERTS "%CA_CERT%" >nul 2>&1

:: 强制 Git 使用 Windows 原生证书信任库 (Schannel)，彻底解决 Git SSL 证书报错问题
git config --global http.sslBackend schannel >nul 2>&1

echo cacert="%CA_CERT:\=/%" > "%USERPROFILE%\_curlrc"
echo ssl-no-revoke >> "%USERPROFILE%\_curlrc"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v CertificateRevocation /t REG_DWORD /d 0 /f >nul 2>&1

echo [OK] CA injected and Curl/PIP configured.
goto DEPLOY_SUCCESS

:CA_FAILED
echo [WARNING] Failed to generate CA Certificate.

:DEPLOY_SUCCESS
echo.
echo ==========================================
echo   SUCCESS: PYTHON SUPER BRIDGE DEPLOYED!
echo ==========================================
echo   1. 请手动重启你的 Chrome/Edge 浏览器以让插件连接生效。
echo   2. 前往 edge://extensions/ 刷新 Proxy Bridge。
echo   3. 核心提示：请打开项目目录下的 settings.json，填入你的真实服务器 IP 和 LLM Key！
echo ==========================================
pause
exit /b