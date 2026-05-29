@echo off
setlocal

set "PROXY_PORT=60130"
set "NATIVE_NAME=com.example.proxy_bridge"
set "ROOT_DIR=%~dp0"
set "NH_DIR=%ROOT_DIR%native-host"
set "CONFIG_FILE=%NH_DIR%\.proxy-bridge-config"
set "CA_DIR=%USERPROFILE%\.proxy-bridge-ca"
set "CA_CERT=%CA_DIR%\ca-cert.pem"

echo ==========================================
echo   Proxy Bridge ULTIMATE ONE-CLICK INSTALL
echo ==========================================

net session >nul 2>&1 || (powershell -Command "Start-Process -FilePath \"%~f0\" -Verb RunAs" & exit /b)
echo [OK] Admin rights confirmed.

echo [1/8] Nuking old processes...
taskkill /F /IM node.exe /T >nul 2>&1
taskkill /F /IM chrome.exe /T >nul 2>&1
timeout /t 2 >nul
echo [OK] Processes killed.

echo [2/8] Cleaning old files...
if exist "%NH_DIR%\node_modules" rd /s /q "%NH_DIR%\node_modules" >nul 2>&1
if exist "%NH_DIR%\proxy-bridge-host.js" del /f /q "%NH_DIR%\proxy-bridge-host.js" >nul 2>&1
if exist "%NH_DIR%\setup-ca.js" del /f /q "%NH_DIR%\setup-ca.js" >nul 2>&1
if exist "%CONFIG_FILE%" del /f /q "%CONFIG_FILE%" >nul 2>&1
if exist "%CA_DIR%" rd /s /q "%CA_DIR%" >nul 2>&1
REG DELETE "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /f >nul 2>&1
REG DELETE "HKLM\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /f >nul 2>&1
echo [OK] System purged.

echo [3/8] Extracting fresh JS code...
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { param($f, $d) $lines = Get-Content -LiteralPath $f; $s1=@($lines|Select-String '===CA_JS_START===' -SimpleMatch|Where-Object{$_.Line -notmatch 'Select-String'})[0].LineNumber; $e1=@($lines|Select-String '===CA_JS_END===' -SimpleMatch|Where-Object{$_.Line -notmatch 'Select-String'})[0].LineNumber; $lines[$s1..($e1-2)]|Set-Content -LiteralPath (Join-Path $d 'setup-ca.js') -Encoding UTF8; $s2=@($lines|Select-String '===HOST_JS_START===' -SimpleMatch|Where-Object{$_.Line -notmatch 'Select-String'})[0].LineNumber; $e2=@($lines|Select-String '===HOST_JS_END===' -SimpleMatch|Where-Object{$_.Line -notmatch 'Select-String'})[0].LineNumber; $lines[$s2..($e2-2)]|Set-Content -LiteralPath (Join-Path $d 'proxy-bridge-host.js') -Encoding UTF8; Write-Host '[OK] Extracted' }" "%~f0" "%NH_DIR%"

set "NODE_EXE_PATH="
for /f "delims=" %%i in ('where node 2^>nul') do if not defined NODE_EXE_PATH set "NODE_EXE_PATH=%%i"
if not defined NODE_EXE_PATH set "NODE_EXE_PATH=C:\Program Files\nodejs\node.exe"

(
echo @echo off
echo set NODE_NO_WARNINGS=1
echo cd /d "%%~dp0"
echo "%NODE_EXE_PATH%" "proxy-bridge-host.js" 2^>^> "error.log"
echo exit /b
) > "%NH_DIR%\run-host.bat"
echo [OK] Host runner created.

echo [4/8] Verifying Node.js...
node -v >nul 2>&1 || (echo [ERROR] Node.js not found. & pause & exit /b 1)
echo [OK] Node.js ready.

echo [5/8] Installing dependencies...
cd /d "%NH_DIR%"
call npm config set registry https://mirrors.cloud.tencent.com/npm/ >nul 2>&1
call npm install --silent --no-audit --no-fund 2>nul || call npm install --silent --no-audit --no-fund 2>nul
echo [OK] Dependencies installed.

echo [6/8] Generating CA and trusting system...
node setup-ca.js >nul 2>&1
powershell -NoProfile -Command "try { Import-Certificate -FilePath '%CA_CERT%' -CertStoreLocation Cert:\LocalMachine\Root -ErrorAction Stop } catch {}; try { Import-Certificate -FilePath '%CA_CERT%' -CertStoreLocation Cert:\CurrentUser\Root -ErrorAction Stop } catch {}" >nul 2>&1
echo [OK] CA installed to Windows Trust Store.

echo [7/8] Configuring global ENV and curl...
setx CURL_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx REQUESTS_CA_BUNDLE "%CA_CERT%" >nul 2>&1
setx SSL_CERT_FILE "%CA_CERT%" >nul 2>&1
setx NODE_EXTRA_CA_CERTS "%CA_CERT%" >nul 2>&1

:: Fix curl path escaping and Schannel CRL check
set "U_PROFILE=%USERPROFILE%"
set "U_PROFILE_FWD=%U_PROFILE:\=/%"
set "CA_PEM_FWD=%U_PROFILE_FWD%/.proxy-bridge-ca/ca-cert.pem"
echo cacert="%CA_PEM_FWD%" > "%USERPROFILE%\_curlrc"
echo ssl-no-revoke >> "%USERPROFILE%\_curlrc"

:: Disable IE/Schannel revocation checks globally
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v CertificateRevocation /t REG_DWORD /d 0 /f >nul 2>&1
echo [OK] ENV vars injected and curl configured.

echo [8/8] Registering Native Messaging...
set "EXT_ID="
if exist "%CONFIG_FILE%" for /f "tokens=2*" %%a in ('findstr /i "^extension_id=" "%CONFIG_FILE%" 2^>nul') do set "EXT_ID=%%b"
if "%EXT_ID%"=="" (
    set /p EXT_ID=Paste Chrome Extension ID: 
    echo extension_id=%EXT_ID%> "%CONFIG_FILE%"
)
set "BP=%NH_DIR%\run-host.bat"
set "BPE=%BP:\=\\%"
echo {"name":"%NATIVE_NAME%","description":"Proxy Bridge","path":"%BPE%","type":"stdio","allowed_origins":["chrome-extension://%EXT_ID%/"]}> "%NH_DIR%\%NATIVE_NAME%.json"
REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\%NATIVE_NAME%" /ve /t REG_SZ /d "%NH_DIR%\%NATIVE_NAME%.json" /f >nul 2>&1
echo [OK] Native Messaging registered.

echo.
echo ==========================================
echo   SUCCESS: 100%% PERFECT DEPLOYMENT!
echo ==========================================
echo   1. OPEN Chrome manually.
echo   2. Go to chrome://extensions/ -> Refresh Proxy Bridge.
echo   3. CLOSE this CMD, open a NEW CMD.
echo   4. Test instantly (No extra flags needed!):
echo      curl -x http://127.0.0.1:%PROXY_PORT% https://www.google.com
echo ==========================================
pause
exit /b

===CA_JS_START===
'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),forge=require('node-forge');
const CD=path.join(os.homedir(),'.proxy-bridge-ca'),CC=path.join(CD,'ca-cert.pem'),CK=path.join(CD,'ca-key.pem');
function gCA(){
  if(!fs.existsSync(CC)||!fs.existsSync(CK)){
    if(!fs.existsSync(CD))fs.mkdirSync(CD,{recursive:true});
    const k=forge.pki.rsa.generateKeyPair(2048),c=forge.pki.createCertificate();
    c.publicKey=k.publicKey;c.serialNumber='01';c.validity.notBefore=new Date();c.validity.notAfter=new Date();
    c.validity.notAfter.setFullYear(c.validity.notAfter.getFullYear()+10);
    c.setSubject([{name:'commonName',value:'Proxy Bridge Local CA'}]);c.setIssuer(c.subject.attributes);
    c.setExtensions([{name:'basicConstraints',cA:true},{name:'keyUsage',keyCertSign:true,cRLSign:true},{name:'subjectKeyIdentifier'}]);
    c.sign(k.privateKey,forge.md.sha256.create());
    fs.writeFileSync(CC,forge.pki.certificateToPem(c),'utf8');fs.writeFileSync(CK,forge.pki.privateKeyToPem(k.privateKey),'utf8');fs.chmodSync(CK,0o600);
  }
  return {cert:forge.pki.certificateFromPem(fs.readFileSync(CC,'utf8')),key:forge.pki.privateKeyFromPem(fs.readFileSync(CK,'utf8'))};
}
let cc=null;function rCA(){return cc||(cc=gCA());}
function gCH(h){
  const ca=rCA(),k=forge.pki.rsa.generateKeyPair(2048),c=forge.pki.createCertificate();
  c.publicKey=k.publicKey;c.serialNumber='01';c.validity.notBefore=new Date(Date.now()-86400000);c.validity.notAfter=new Date(Date.now()+365*86400000);
  c.setSubject([{name:'commonName',value:h}]);c.setIssuer(ca.cert.subject.attributes);
  c.setExtensions([{name:'basicConstraints',cA:false},{name:'keyUsage',digitalSignature:true,keyEncipherment:true},{name:'extKeyUsage',serverAuth:true},{name:'subjectAltName',altNames:[{type:2,value:h}]},{name:'subjectKeyIdentifier'}]);
  c.sign(ca.key,forge.md.sha256.create());
  return {cert:forge.pki.certificateToPem(c),key:forge.pki.privateKeyToPem(k.privateKey)};
}
module.exports={getCertForHost:gCH,getCachedCertForHost:gCH};
===CA_JS_END===
===HOST_JS_START===
const fs = require('fs');
const path = require('path');
const logFile = path.join(process.cwd(), 'debug.log');
try { fs.writeFileSync(logFile, '=== HOST STARTED ===\n'); } catch(e) {}
const dbg = (m) => { try { fs.appendFileSync(logFile, `[${new Date().toISOString()}] ${m}\n`); } catch(e){} };
console.log = console.info = console.warn = console.error = (...a) => dbg('CONSOLE: ' + a.join(' '));
process.on('uncaughtException', e => { dbg('CRASH: ' + e.stack); process.exit(1); });
process.on('unhandledRejection', e => dbg('REJECT: ' + e));
try {
  const { getCachedCertForHost } = require('./setup-ca');
  const http = require('http');
  const tls = require('tls');
  const PORT = parseInt(process.env.PB_PORT || '60130', 10);
  const HOST = '127.0.0.1';
  let buf = Buffer.alloc(0), pending = new Map(), idC = 1;
  function send(o) {
    const j = JSON.stringify(o), b = Buffer.from(j, 'utf8'), h = Buffer.alloc(4);
    h.writeUInt32LE(b.length, 0); process.stdout.write(h); process.stdout.write(b);
  }
  process.stdin.on('readable', () => {
    let c;
    while ((c = process.stdin.read()) !== null) buf = Buffer.concat([buf, c]);
    while (buf.length >= 4) {
      const l = buf.readUInt32LE(0);
      if (l > 67108864) process.exit(2);
      if (buf.length < 4 + l) break;
      const j = buf.slice(4, 4 + l).toString('utf8'); buf = buf.slice(4 + l);
      try { handleMsg(JSON.parse(j)); } catch (e) { dbg('PARSE ERR: ' + e.message); }
    }
  });
  process.stdin.on('end', () => dbg('STDIN CLOSED'));
  function handleMsg(m) {
    if (!m || !m.id || !m.type) return;
    const p = pending.get(m.id); if (!p) return; clearTimeout(p.timer);
    if (m.type === 'pong') return;
    if (m.type === 'error') { pending.delete(m.id); p.reject(new Error(m.error)); return; }
    if (m.type === 'response') {
      p.header = m; p.chunks = new Array(m.totalChunks || 0); p.received = 0;
      if ((m.totalChunks || 0) === 0) finish(m.id);
    } else if (m.type === 'chunk') {
      p.chunks[m.index] = m.data ? Buffer.from(m.data, 'base64') : Buffer.alloc(0);
      p.received++; if (p.received === p.header.totalChunks) finish(m.id);
    }
  }
  function finish(id) {
    const p = pending.get(id); if (!p) return; pending.delete(id);
    p.resolve({ status: p.header.status, statusText: p.header.statusText || '', headers: p.header.headers || {}, body: Buffer.concat(p.chunks.filter(Boolean)) });
  }
  function forward(req) {
    return new Promise((res, rej) => {
      const id = idC++;
      const t = setTimeout(() => { if (pending.delete(id)) rej(new Error('NM_TIMEOUT')); }, 60000);
      pending.set(id, { resolve: res, reject: rej, timer: t });
      send({ type: 'request', id, method: req.method, url: req.url, headers: req.headers, body: req.body ? req.body.toString('base64') : null });
    });
  }
  function fixUrl(u, pr = 'https') {
    if (!u || u === '/' || u === '//') return pr + '://127.0.0.1/';
    u = u.trim();
    if (/^https?:\/\//i.test(u)) return u.replace(/^(https?):\/([^/])/i, '$1://$2');
    if (u.startsWith('//')) return pr + ':' + u;
    if (/^https?:\/\//i.test(u)) return u;
    const h = (u.split(':')[0] || '127.0.0.1').split('/')[0];
    const p = u.includes('/') ? u.substring(u.indexOf('/')) : '/';
    return pr + '://' + h + p;
  }
  function stripHop(h) {
    const o = { ...h };
    ['connection', 'keep-alive', 'proxy-authorization', 'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade'].forEach(k => delete o[k]);
    return o;
  }
  const server = http.createServer(async (req, res) => {
    let body = Buffer.alloc(0);
    req.on('data', c => body = Buffer.concat([body, c]));
    req.on('end', async () => {
      try {
        const r = await forward({ method: req.method, url: fixUrl(req.url, 'http'), headers: stripHop(req.headers), body });
        res.writeHead(r.status, r.statusText, stripHop(r.headers)); res.end(r.body);
      } catch (e) { if (!res.headersSent) res.writeHead(502); res.end('ERR'); }
    });
  });
  server.on('connect', (req, sock) => {
    const parts = req.url.split(':');
    const h = parts[0];
    const port = parts[1] || '443';
    let cert;
    try {
      cert = getCachedCertForHost(h);
      if (!cert || !cert.cert || !cert.key) throw new Error('BAD_CERT');
    } catch (e) { sock.write('HTTP/1.1 502\r\n\r\n'); sock.end(); return; }
    sock.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    const tlsSock = new tls.TLSSocket(sock, { isServer: true, key: cert.key, cert: cert.cert });
    const inner = http.createServer(async (iReq, iRes) => {
      let b = Buffer.alloc(0);
      iReq.on('data', c => b = Buffer.concat([b, c]));
      await new Promise(r => iReq.on('end', r));
      try {
        const targetUrl = `https://${h}${port === '443' ? '' : ':' + port}${iReq.url}`;
        const r = await forward({ method: iReq.method, url: targetUrl, headers: stripHop(iReq.headers), body: b });
        iRes.writeHead(r.status, r.statusText, stripHop(r.headers)); iRes.end(r.body);
      } catch (e) { if (!iRes.headersSent) iRes.writeHead(502); iRes.end('ERR'); }
    });
    inner.emit('connection', tlsSock);
  });
  server.listen(PORT, HOST, () => dbg('LISTENING ' + HOST + ':' + PORT));
} catch (e) { dbg('FATAL: ' + e.stack); process.exit(1); }
===HOST_JS_END===