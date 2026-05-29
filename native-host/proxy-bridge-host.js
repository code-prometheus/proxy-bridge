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
