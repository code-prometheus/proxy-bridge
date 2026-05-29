@echo off
set NODE_NO_WARNINGS=1
cd /d "%~dp0"
"C:\Program Files\nodejs\node.exe" "proxy-bridge-host.js" 2>> "error.log"
exit /b
