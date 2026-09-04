@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
set "NH_DIR=%ROOT%chrome-native-config"
set "CA_DIR=%USERPROFILE%\.proxy-bridge-ca"
set "CA_CERT=%CA_DIR%\ca-cert.pem"
set "NATIVE_NAME=com.example.proxy_bridge"

echo.
echo ==========================================
echo   Proxy Bridge v2.0 - One-Click Setup
echo ==========================================
echo.

:: Admin check
net session >nul 2>&1
if %errorlevel% equ 0 goto :ADMIN_OK
echo [*] Requesting Administrator privileges...
powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:ADMIN_OK
echo [OK] Admin confirmed.

:: Python check
echo [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

:: Pip install cryptography
echo [2/5] Installing cryptography...
python -m pip install cryptography --quiet 2>nul
if %errorlevel% equ 0 goto :PIP_OK
python -m pip install cryptography -i https://mirrors.cloud.tencent.com/pypi/simple/ --trusted-host mirrors.cloud.tencent.com --quiet 2>nul
if %errorlevel% equ 0 goto :PIP_OK
python -m pip install cryptography -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn --quiet 2>nul
if %errorlevel% equ 0 goto :PIP_OK
echo [ERROR] Cannot install cryptography. Run: pip install cryptography
pause
exit /b 1

:PIP_OK
echo [OK] cryptography ready.

:: CA Certificate
echo [3/5] Generating CA Certificate...
python "%ROOT%entry.py" --init-ca 2>nul
if not exist "%CA_CERT%" (
    echo [ERROR] CA generation failed.
    pause
    exit /b 1
)
echo [OK] CA: %CA_CERT%

:: Install CA to Windows Trust Store
echo [4/5] Installing CA to Windows Trust Store...
certutil -addstore -f "Root" "%CA_CERT%" >nul 2>&1
certutil -addstore -f -user "Root" "%CA_CERT%" >nul 2>&1
echo [OK] CA installed to system root store.

:: Env vars
setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
setx NODE_EXTRA_CA_CERTS "%CA_CERT%" >nul 2>&1
setx PIP_CERT "%CA_CERT%" >nul 2>&1
git config --global http.sslBackend schannel >nul 2>&1
echo [OK] Env vars set for curl, pip, node, git.

:: Native Messaging
echo [5/5] Configuring Chrome Native Messaging...

set "RUN_BAT=%NH_DIR%\run-host.bat"
set "RUN_BAT_ESC=%RUN_BAT:\=\\%"

set "EXT_ID="
set /p EXT_ID="Paste your Chrome Extension ID: "
if "!EXT_ID!"=="" (
    echo [WARNING] No ID provided. Run setup again when you have the ID.
    goto :DONE
)

echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%RUN_BAT_ESC%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%NH_DIR%\%NATIVE_NAME%.json"

REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1
REG ADD "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1
echo [OK] Native Messaging registered for: %EXT_ID%

:DONE
echo.
echo ==========================================
echo   Proxy Bridge v2.0 Setup Complete!
echo ==========================================
echo.
echo   Proxy: 127.0.0.1:60130
echo   CA:    %CA_CERT%
echo.
echo   Next: Restart Chrome to activate.
echo   The extension auto-starts the proxy!
echo.
echo   Usage:
echo     set http_proxy=http://127.0.0.1:60130
echo     set https_proxy=http://127.0.0.1:60130
echo ==========================================
pause
exit /b 0
