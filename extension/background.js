/**
 * Proxy Bridge - Background Service Worker (Manifest V3)
 * 负责：连接 Native Host、转发 fetch 请求、分块处理响应、状态维护
 */

// ========== 配置 ==========
const NATIVE_HOST_NAME = 'com.example.proxy_bridge';
const CHUNK_SIZE = 256 * 1024; // 256KB 分块
const RECONNECT_DELAY = 3000;

// ========== 状态 ==========
let nmPort = null;
let reconnectTimer = null;

// ========== 工具函数 ==========
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

// ========== 处理来自 Native Host 的请求 ==========
async function handleRequest(msg) {
  const { id, method, url, headers, body } = msg;
  const start = Date.now();
  try {
    const fetchOpts = {
      method,
      headers: filterHopByHop(headers),
      redirect: 'follow',
      credentials: 'omit',
      cache: 'no-store'
    };

    if (body && !['GET', 'HEAD'].includes(method.toUpperCase())) {
      fetchOpts.body = base64ToUint8(body);
    }

    // 🔑 核心：fetch 会走 Chrome 当前代理设置 & 插件已有认证链
    const resp = await fetch(url, fetchOpts);
    const buf = new Uint8Array(await resp.arrayBuffer());
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });

    const totalChunks = Math.max(1, Math.ceil(buf.length / CHUNK_SIZE));

    // 1. 发送响应头
    safeSend({
      type: 'response',
      id,
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders,
      totalChunks,
      contentLength: buf.length
    });

    // 2. 分块发送 Body
    if (buf.length === 0) {
      safeSend({ type: 'chunk', id, index: 0, data: '' });
    } else {
      for (let i = 0; i < totalChunks; i++) {
        const slice = buf.subarray(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
        safeSend({
          type: 'chunk',
          id,
          index: i,
          data: uint8ToBase64(slice)
        });
      }
    }

    console.log(`[PB] ✅ ${method} ${url} -> ${resp.status} (${buf.length}B, ${Date.now() - start}ms)`);
  } catch (err) {
    console.error(`[PB] ❌ ${method} ${url} ERROR:`, err);
    safeSend({ type: 'error', id, error: err.message || String(err) });
  }
}

// ========== Native Messaging 连接管理 ==========
function connect() {
  if (nmPort) return;
  try {
    nmPort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (e) {
    console.error('[PB] connectNative failed:', e);
    scheduleReconnect();
    return;
  }

  nmPort.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    if (msg.type === 'request') handleRequest(msg);
    else if (msg.type === 'ping') safeSend({ type: 'pong', ts: Date.now() });
  });

  nmPort.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    console.warn('[PB] 🔌 disconnected:', err?.message || 'normal');
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

// ========== 生命周期钩子 ==========
chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect();

// 心跳保活（防止 Service Worker 休眠导致 NM 断开）
setInterval(() => {
  if (nmPort) safeSend({ type: 'ping', ts: Date.now() });
}, 25_000);

// ========== 对外状态接口（供 popup.js 查询） ==========
chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === 'status') {
    sendResponse({ connected: !!nmPort });
  }
  return true;
});