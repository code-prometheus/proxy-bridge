@echo off
setlocal

set "NATIVE_NAME=com.example.proxy_bridge"
set "ROOT_DIR=%~dp0"
set "NH_DIR=%ROOT_DIR%chrome-native-config"
set "CONFIG_FILE=%ROOT_DIR%config.ini"
set "CA_DIR=%USERPROFILE%\.proxy-bridge-ca"
set "CA_CERT=%CA_DIR%\ca-cert.pem"
set "PY_SCRIPT=%ROOT_DIR%tunnel_client_and_remote_proxy.py"

echo ==========================================
echo   Super Bridge: Python Dual-Engine Setup
echo ==========================================

net session >nul 2>&1 || (powershell -Command "Start-Process -FilePath \"%~f0\" -Verb RunAs" & exit /b)
echo [OK] Admin rights confirmed.

if not exist "%PY_SCRIPT%" (
    echo [ERROR] 找不到核心文件: tunnel_client_and_remote_proxy.py
    echo 请确保该文件与本 bat 脚本放在同一目录下！
    pause
    exit /b 1
)

echo [1/7] Checking Python Environment...
python --version >nul 2>&1 || (echo [ERROR] Python not found in PATH! Please install Python 3.8+ & pause & exit /b 1)

echo [2/7] Nuking old processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM node.exe /T >nul 2>&1
taskkill /F /IM chrome.exe /T >nul 2>&1
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
pip install cryptography -i https://mirrors.cloud.tencent.com/pypi/simple/ >nul 2>&1
echo [OK] Python dependencies ready.

echo [5/7] Generating Run Batch for Chrome Native Messaging...
(
echo @echo off
echo cd /d "%%~dp0.."
echo python "tunnel_client_and_remote_proxy.py"
) > "%NH_DIR%\run-host.bat"

if not exist "%CONFIG_FILE%" (
    (
    echo [common]
    echo secret_key = Quantitative_Trading_Tunnel_2026
    echo.
    echo [client]
    echo server_addr = 122.1.17.123
    echo server_port = 6974
    ) > "%CONFIG_FILE%"
)

echo [6/7] Registering Native Messaging...
set "EXT_ID="
set /p EXT_ID=Paste Chrome Extension ID: 

set "BP=%NH_DIR%\run-host.bat"
set "BPE=%BP:\=\\%"
echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%BPE%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%NH_DIR%\%NATIVE_NAME%.json"
REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1

echo [7/7] Generating and Injecting CA to System...
:: Trigger initial Python run to generate CA
python "%PY_SCRIPT%" --init-ca >nul 2>&1
timeout /t 3 >nul

if exist "%CA_CERT%" (
    powershell -NoProfile -Command "try { Import-Certificate -FilePath '%CA_CERT%' -CertStoreLocation Cert:\LocalMachine\Root -ErrorAction Stop } catch {}; try { Import-Certificate -FilePath '%CA_CERT%' -CertStoreLocation Cert:\CurrentUser\Root -ErrorAction Stop } catch {}" >nul 2>&1
    setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
    setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
    setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
    setx PIP_CERT "%CA_CERT%" >nul 2>&1
    echo cacert="%CA_CERT:\=/%" > "%USERPROFILE%\_curlrc"
    echo ssl-no-revoke >> "%USERPROFILE%\_curlrc"
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v CertificateRevocation /t REG_DWORD /d 0 /f >nul 2>&1
    echo [OK] CA injected and Curl/PIP configured.
) else (
    echo [WARNING] Failed to generate CA Certificate.
)

echo.
echo ==========================================
echo   SUCCESS: PYTHON SUPER BRIDGE DEPLOYED!
echo ==========================================
echo   1. OPEN Chrome manually.
echo   2. Go to chrome://extensions/ -^> Refresh Proxy Bridge.
echo   3. Everything is fully automated now!
echo ==========================================
pause
exit /b