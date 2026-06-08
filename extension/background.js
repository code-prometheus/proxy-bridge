/**
 * Proxy Bridge - Background Service Worker (Manifest V3)
 * 完美融合：无限大流量 POST 突破 + 实时流式响应 (SSE)
 */
const NATIVE_HOST_NAME = 'com.example.proxy_bridge';
const CHUNK_SIZE = 256 * 1024;
const RECONNECT_DELAY = 3000;

let nmPort = null;
let reconnectTimer = null;
const pendingRequests = {};

function uint8ToBase64(u8) {
  let bin = '';
  const BLOCK = 0x8000;
  for (let i = 0; i < u8.length; i += BLOCK) {
    bin += String.fromCharCode.apply(null, u8.subarray(i, i + BLOCK));
  }
  return btoa(bin);
}

function base64ToUint8(b64) {
  const bin = atob(b64);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}

function filterHopByHop(headers) {
  const drop = new Set(['host', 'connection', 'keep-alive', 'proxy-authorization', 'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade']);
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    if (!drop.has(k.toLowerCase())) out[k] = v;
  }
  return out;
}

function safeSend(msg) {
  try {
    if (nmPort) nmPort.postMessage(msg);
  } catch (e) {
    console.error('[PB] NM send failed:', e);
  }
}

async function handleRequest(msg) {
  const { id, method, url, headers, body, _u8Body } = msg;
  const start = Date.now();
  try {
    const fetchOpts = {
      method,
      headers: filterHopByHop(headers),
      redirect: 'follow',
      credentials: 'omit',
      cache: 'no-store'
    };

    // 优先使用切片组装的无损大体积 Uint8Array
    if (_u8Body) {
      fetchOpts.body = _u8Body;
    } else if (body && !['GET', 'HEAD'].includes(method.toUpperCase())) {
      fetchOpts.body = base64ToUint8(body);
    }

    const resp = await fetch(url, fetchOpts);
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });

    safeSend({
      type: 'response',
      id,
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders
    });

    if (resp.body) {
      const reader = resp.body.getReader();
      let chunkIndex = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          const totalSlices = Math.ceil(value.length / CHUNK_SIZE);
          for (let i = 0; i < totalSlices; i++) {
            const slice = value.subarray(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
            safeSend({
              type: 'chunk',
              id,
              index: chunkIndex++,
              data: uint8ToBase64(slice)
            });
          }
        }
      }
    }

    safeSend({ type: 'end', id });
    console.log(`[PB] ✅ ${method} ${url} -> ${resp.status} (Streamed, ${Date.now() - start}ms)`);
  } catch (err) {
    console.error(`[PB] ❌ ${method} ${url} ERROR:`, err);
    safeSend({ type: 'error', id, error: err.message || String(err) });
  }
}

function connect() {
  if (nmPort) return;
  try {
    nmPort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (e) {
    scheduleReconnect();
    return;
  }

  nmPort.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    
    // 💡 核心协议升级：处理流式分块上传，突破 Git 大文件 1MB 极限
    if (msg.type === 'request') {
      handleRequest(msg);
    } else if (msg.type === 'request_start') {
      pendingRequests[msg.id] = { id: msg.id, method: msg.method, url: msg.url, headers: msg.headers, chunks: [], totalLen: 0 };
    } else if (msg.type === 'request_chunk') {
      if (pendingRequests[msg.id]) {
        const u8 = base64ToUint8(msg.data);
        pendingRequests[msg.id].chunks.push(u8);
        pendingRequests[msg.id].totalLen += u8.length;
      }
    } else if (msg.type === 'request_end') {
      const req = pendingRequests[msg.id];
      delete pendingRequests[msg.id];
      if (!req) return;
      
      let fullBody = null;
      if (req.totalLen > 0) {
        fullBody = new Uint8Array(req.totalLen);
        let offset = 0;
        for (const u8 of req.chunks) {
          fullBody.set(u8, offset);
          offset += u8.length;
        }
      }
      handleRequest({ id: req.id, method: req.method, url: req.url, headers: req.headers, _u8Body: fullBody });
    } else if (msg.type === 'ping') {
      safeSend({ type: 'pong', ts: Date.now() });
    }
  });

  nmPort.onDisconnect.addListener(() => {
    nmPort = null;
    scheduleReconnect();
  });
  console.log('[PB] 🔗 connected to native host');
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY);
}

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect();

setInterval(() => {
  if (nmPort) safeSend({ type: 'ping', ts: Date.now() });
}, 25_000);

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === 'status') sendResponse({ connected: !!nmPort });
  return true;
});