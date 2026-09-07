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
echo Proxy Bridge v2.0 — One-Click Setup
echo MITM TLS + Chrome NM + ghelper
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

:: ── 1/5 Python ───────────────────────────────────────────────────────────
echo [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
 echo [ERROR] Python 3.8+ required. Install from https://python.org first.
 pause
 exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [OK] Python %%v

:: Generate run-host.bat with auto-detected Python path
for /f "delims=" %%i in ('where python 2^>nul') do (
 set "PYTHON_PATH=%%i"
 goto :PYTHON_FOUND
)
:PYTHON_FOUND
echo @echo off> "%NH_DIR%\run-host.bat"
echo cd /d "%ROOT%"^&^& "%PYTHON_PATH%" "%~dp0entry.py">> "%NH_DIR%\run-host.bat"
echo [OK] run-host.bat: %PYTHON_PATH%

:: ── 2/5 cryptography ─────────────────────────────────────────────────────
echo [2/5] Installing cryptography...
python -m pip install cryptography --quiet 2>nul
if %errorlevel% neq 0 (
 python -m pip install cryptography -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn --quiet 2>nul
)
echo [OK] cryptography ready.

:: ── 3/5 CA Certificate ───────────────────────────────────────────────────
echo [3/5] Generating Root CA...
python "%ROOT%entry.py" --init-ca 2>nul
if not exist "%CA_CERT%" (
 echo [ERROR] CA generation failed.
 pause
 exit /b 1
)
echo [OK] CA: %CA_CERT%

:: ── 4/5 Install CA to Windows Trust Store ─────────────────────────────────
echo [4/5] Installing CA to Windows Trust Store...
certutil -addstore -f "Root" "%CA_CERT%" >nul 2>&1
certutil -addstore -f -user "Root" "%CA_CERT%" >nul 2>&1
echo [OK] CA installed.

:: ── 4.5/5 Compute Extension ID from manifest key ──────────────────────────
echo [4.5/5] Computing Chrome Extension ID from manifest key...
for /f "delims=" %%i in ('python -c "import json,hashlib,base64; m=json.load(open(r'%ROOT%extension\manifest.json','rb')); k=m['key']; d=base64.b64decode(k); h=hashlib.sha256(d).digest()[:16]; a='abcdefghijklmnop'; print(''.join(a[b>>4]+a[b&0x0f] for b in h))"') do set "EXT_ID=%%i"
if "!EXT_ID!"=="" (
 echo [ERROR] Failed to compute extension ID from manifest key.
 pause
 exit /b 1
)
echo [OK] Extension ID: !EXT_ID!

:: ── 5/5 Chrome Native Messaging ──────────────────────────────────────────
echo [5/5] Registering Chrome Native Messaging...
set "MANIFEST=%NH_DIR%\%NATIVE_NAME%.json"
set "RUN_BAT=%NH_DIR%\run-host.bat"
set "RUN_BAT_ESC=%RUN_BAT:\=\\%"

echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%RUN_BAT_ESC%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%MANIFEST%"

REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%MANIFEST%" /f
REG ADD "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%MANIFEST%" /f
echo [OK] NM registered for extension: %EXT_ID%

:: ── Summary ──────────────────────────────────────────────────────────────
echo.
echo ==========================================
echo Setup Complete! Proxy Bridge v2.0
echo ==========================================
echo.
echo Proxy Address: 127.0.0.1:60130 (remote: 0.0.0.0:60130)
echo Extension ID:   %EXT_ID%
echo NM Host:        %NATIVE_NAME%
echo CA:             %CA_CERT%
echo.
echo Quick Start:
echo 1. Load unpacked extension in Chrome from: %ROOT%extension\
echo 2. Restart Chrome
echo 3. set http_proxy=http://127.0.0.1:60130
echo 4. curl https://platform.worldquantbrain.com/sign-in
echo.
pause
exit /b 0
