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
echo   Proxy Bridge v2.0 — One-Click Setup
echo   MITM TLS + Chrome NM + ghelper
echo ==========================================
echo.

:: ── Admin check ──────────────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% equ 0 goto :ADMIN_OK
echo [*] Requesting Administrator privileges...
powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:ADMIN_OK
echo [OK] Admin confirmed.

:: ── 1/6 Python ───────────────────────────────────────────────────────────
echo [1/6] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.8+ required. Install from https://python.org first.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [OK] Python %%v

:: ── 2/6 cryptography ─────────────────────────────────────────────────────
echo [2/6] Installing cryptography (SSL cert generation)...
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

:: ── 3/6 CA Certificate ───────────────────────────────────────────────────
echo [3/6] Generating Root CA (Proxy Bridge Local CA)...
python "%ROOT%entry.py" --init-ca 2>nul
if not exist "%CA_CERT%" (
    echo [ERROR] CA generation failed.
    pause
    exit /b 1
)
echo [OK] CA: %CA_CERT%

:: ── 4/6 Install CA to Windows Trust Store ─────────────────────────────────
echo [4/6] Installing CA to Windows Trust Store...
certutil -addstore -f "Root" "%CA_CERT%" >nul 2>&1
certutil -addstore -f -user "Root" "%CA_CERT%" >nul 2>&1
echo [OK] CA installed. All apps will trust proxy-signed certificates.

:: ── 5/6 Environment Variables ─────────────────────────────────────────────
echo [5/6] Setting environment variables...
setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
setx NODE_EXTRA_CA_CERTS "%CA_CERT%" >nul 2>&1
setx PIP_CERT "%CA_CERT%" >nul 2>&1
git config --global http.sslBackend schannel >nul 2>&1

:: Also set for current session (setx only affects new terminals)
set CURL_CA_BUNDLE=%CA_CERT%
set REQUESTS_CA_BUNDLE=%CA_CERT%
set SSL_CERT_FILE=%CA_CERT%
set NODE_EXTRA_CA_CERTS=%CA_CERT%
set PIP_CERT=%CA_CERT%
echo [OK] Env vars set (curl, pip, node, git, python).

:: ── 6/6 Chrome Native Messaging ──────────────────────────────────────────
echo [6/6] Configuring Chrome Native Messaging...

set "RUN_BAT=%NH_DIR%\run-host.bat"
set "RUN_BAT_ESC=%RUN_BAT:\=\\%"

:: Try to read existing extension ID from current manifest
set "EXT_ID="
set "MANIFEST=%NH_DIR%\%NATIVE_NAME%.json"
if exist "%MANIFEST%" (
    for /f "tokens=2 delims=/" %%i in ('findstr "chrome-extension" "%MANIFEST%" 2^>nul') do (
        set "EXISTING_ID=%%i"
    )
)
if not "!EXISTING_ID!"=="" (
    echo [*] Found existing extension ID: !EXISTING_ID!
    set /p USE_EXISTING="Use this ID? [Y/n]: "
    if /i "!USE_EXISTING!" neq "n" set "EXT_ID=!EXISTING_ID!"
)

if "!EXT_ID!"=="" (
    set /p EXT_ID="Paste your Chrome Extension ID: "
)

if "!EXT_ID!"=="" (
    echo [WARNING] No extension ID. Run setup again after loading the extension in Chrome.
    goto :SKIP_NM
)

echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%RUN_BAT_ESC%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%MANIFEST%"

REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%MANIFEST%" /f >nul 2>&1
REG ADD "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%MANIFEST%" /f >nul 2>&1
echo [OK] Native Messaging registered for: %EXT_ID%

:SKIP_NM

:: ── Summary ──────────────────────────────────────────────────────────────
echo.
echo ==========================================
echo   Setup Complete! Proxy Bridge v2.0
echo ==========================================
echo.
echo   Proxy Address:  127.0.0.1:60130
echo   Root CA:        %CA_CERT%
echo   NM Host:        %NATIVE_NAME%
echo.
echo   Architecture:
echo   Client --^> 127.0.0.1:60130 --^>
echo     CONNECT https:// -- MITM TLS ^(local CA^) --^> Chrome NM --^> ghelper --^> Internet
echo     GET http:// -- direct forward --^> Internet
echo.
echo   Quick Start:
echo   1. Restart Chrome to activate the extension
echo   2. Test: set http_proxy=http://127.0.0.1:60130
echo   3. Test: set https_proxy=http://127.0.0.1:60130
echo   4. curl https://platform.worldquantbrain.com/sign-in
echo      ^(should work without -k, SSL verify OK^)
echo.
echo   IMPORTANT: This terminal needs restart for setx env vars.
echo   The current session already has CA vars set.
echo.
pause
exit /b 0
