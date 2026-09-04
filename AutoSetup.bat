@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "NH_DIR=%ROOT_DIR%chrome-native-config"
set "CA_DIR=%USERPROFILE%\.proxy-bridge-ca"
set "CA_CERT=%CA_DIR%\ca-cert.pem"
set "NATIVE_NAME=com.example.proxy_bridge"

echo.
echo ==========================================
echo   Proxy Bridge v2.0 — One-Click Setup
echo ==========================================
echo.

:: ============ STEP 0: Admin check ============
net session >nul 2>&1
if not errorlevel 1 goto ADMIN_OK
echo [*] Requesting Administrator privileges...
powershell -Command "Start-Process -FilePath \"%~f0\" -Verb RunAs"
exit /b

:ADMIN_OK
echo [OK] Administrator privileges confirmed.
echo.

:: ============ STEP 1: Python dependency ============
echo [1/5] Installing Python dependency (cryptography)...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3.8+ first.
    pause
    exit /b 1
)

:: Save original proxy settings
set "ORIG_HTTP_PROXY=%http_proxy%"
set "ORIG_HTTPS_PROXY=%https_proxy%"
set "http_proxy="
set "https_proxy="

python -m pip install cryptography -i https://mirrors.cloud.tencent.com/pypi/simple/ --trusted-host mirrors.cloud.tencent.com >nul 2>&1
if errorlevel 1 (
    python -m pip install cryptography -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn >nul 2>&1
)
if errorlevel 1 (
    set "http_proxy=!ORIG_HTTP_PROXY!"
    set "https_proxy=!ORIG_HTTPS_PROXY!"
    python -m pip install cryptography >nul 2>&1
)
if errorlevel 1 (
    echo [ERROR] Failed to install cryptography. Run manually: pip install cryptography
    pause
    exit /b 1
)
echo [OK] cryptography installed.
echo.

:: ============ STEP 2: CA Certificate ============
echo [2/5] Generating CA Certificate...
python "%ROOT_DIR%entry.py" --init-ca
if not exist "%CA_CERT%" (
    echo [ERROR] CA certificate generation failed!
    pause
    exit /b 1
)
echo [OK] CA certificate generated at: %CA_CERT%
echo.

:: ============ STEP 3: Install CA to System ============
echo [3/5] Installing CA to Windows Trust Store...
certutil -addstore -f "Root" "%CA_CERT%" >nul 2>&1
certutil -addstore -f -user "Root" "%CA_CERT%" >nul 2>&1
echo [OK] CA certificate installed to system root store.

:: Set environment variables for common tools
setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
setx NODE_EXTRA_CA_CERTS "%CA_CERT%" >nul 2>&1
setx PIP_CERT "%CA_CERT%" >nul 2>&1

:: Git SSL via Windows native store
git config --global http.sslBackend schannel >nul 2>&1
echo [OK] Environment variables configured (curl, pip, node, git).
echo.

:: ============ STEP 4: Native Messaging Setup ============
echo [4/5] Configuring Chrome Native Messaging...

set "RUN_BAT=%NH_DIR%\run-host.bat"
set "RUN_BAT_ESC=%RUN_BAT:\=\\%"

:: Ask for Extension ID
set "EXT_ID="
set /p EXT_ID="Enter your Chrome Extension ID: "
if "!EXT_ID!"=="" (
    echo [WARNING] No Extension ID provided. You'll need to configure this manually.
    echo [INFO] Load extension/chrome://extensions/ first to get the ID.
    goto SKIP_NM
)

:: Generate Native Messaging manifest
echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%RUN_BAT_ESC%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%NH_DIR%\%NATIVE_NAME%.json"

:: Register in HKCU (user-level)
REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1
:: Also register in HKLM (machine-level, for Edge/Chromium)
REG ADD "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1

echo [OK] Native Messaging registered for extension: %EXT_ID%

:SKIP_NM
echo.

:: ============ STEP 5: Restart Chrome ============
echo [5/5] Setup Complete!
echo.
echo ==========================================
echo   ✓ Proxy Bridge v2.0 Setup Complete!
echo ==========================================
echo.
echo   Proxy Address: 127.0.0.1:60130
echo   CA Location:   %CA_CERT%
echo.
echo   NEXT STEPS:
echo   1. Load extension in Chrome:
echo      chrome://extensions/ → Developer mode → Load unpacked
echo      → Select: %ROOT_DIR%extension\
echo   2. Restart Chrome to activate Native Messaging
echo   3. The extension auto-starts the proxy on launch!
echo   4. Set app proxy to http://127.0.0.1:60130
echo.
echo   For terminal tools (curl, pip, git):
echo   set http_proxy=http://127.0.0.1:60130
echo   set https_proxy=http://127.0.0.1:60130
echo.
echo ==========================================
pause
exit /b 0
