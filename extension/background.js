/**
 * Proxy Bridge - Background Service Worker (Manifest V3)
 * 完美融合：原版高性能 Base64 转换 + 实时流式响应 (SSE)
 */

// ========== 配置 ==========
const NATIVE_HOST_NAME = 'com.example.proxy_bridge';
const CHUNK_SIZE = 256 * 1024; // 256KB 分块防爆
const RECONNECT_DELAY = 3000;

// ========== 状态 ==========
let nmPort = null;
let reconnectTimer = null;

// ========== 工具函数 (沿用原版的高性能分块底层映射) ==========
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

// 沿用原版：过滤禁止的协议头，防止 fetch 抛出核心错误
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

// ========== 核心：处理来自 Native Host 的请求 ==========
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

    const resp = await fetch(url, fetchOpts);
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });

    // 1. 发送响应头 (注：不再发送 totalChunks，因为流是无限的)
    safeSend({
      type: 'response',
      id,
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders
    });

    // 2. 流式分块读取并立即发送 (支持 Claude 的打字机 SSE 特效)
    if (resp.body) {
      const reader = resp.body.getReader();
      let chunkIndex = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        if (value && value.length > 0) {
          // 如果单次读出的数据大于256K，依然使用切片，防止触发 1MB 原生管道限制
          const totalSlices = Math.ceil(value.length / CHUNK_SIZE);
          for (let i = 0; i < totalSlices; i++) {
            const slice = value.subarray(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
            safeSend({
              type: 'chunk',
              id,
              index: chunkIndex++,
              data: uint8ToBase64(slice) // 使用原版的高效函数
            });
          }
        }
      }
    }

    // 3. 流彻底结束，发送结束信号
    safeSend({ type: 'end', id });

    console.log(`[PB] ✅ ${method} ${url} -> ${resp.status} (Streamed, ${Date.now() - start}ms)`);
  } catch (err) {
    console.error(`[PB] ❌ ${method} ${url} ERROR:`, err);
    safeSend({ type: 'error', id, error: err.message || String(err) });
  }
}

// ========== Native Messaging 连接管理 (原版) ==========
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

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect();

setInterval(() => {
  if (nmPort) safeSend({ type: 'ping', ts: Date.now() });
}, 25_000);

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === 'status') {
    sendResponse({ connected: !!nmPort });
  }
  return true;
});