/**
 * Proxy Bridge - Background Service Worker (Manifest V3)
 * 完美融合：无限大流量 POST 突破 + 实时流式响应 (SSE) + 强力心跳保活
 */
const NATIVE_HOST_NAME = 'com.example.proxy_bridge';
const CHUNK_SIZE = 256 * 1024;

let nmPort = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
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
            // 引入极短的微任务暂停，背压缓冲，强制让出主线程，防止大文件导致内存溢出
            await new Promise(r => setTimeout(r, 2));
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
    reconnectAttempts = 0; // 【核心防护】：只要收到消息，立刻清零重试计数器
    if (!msg || !msg.type) return;

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
    } else if (msg.type === 'ping' || msg.type === 'pong') {
      safeSend({ type: 'pong', ts: Date.now() });
    }
  });

  nmPort.onDisconnect.addListener(() => {
    console.warn('[PB] ⚠️ Native messaging port disconnected.', chrome.runtime.lastError);
    nmPort = null;
    scheduleReconnect();
  });
  console.log('[PB] 🔗 connected to native host');
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectAttempts++;
  
  // 【核心修复】：极速抢占策略！
  // 刚断开时采用 200ms 极速重连，在 Chrome 判定后台空闲并挂起前，强行占住一个新的连接！
  // 如果连续失败超过 5 次（说明真的是本地 Python 环境挂了），再退避到 3 秒。
  const delay = reconnectAttempts > 5 ? 3000 : 200;
  
  // 核心防御：在等待重连的间隙，强制调用一次 Chrome 专属 API，彻底打断休眠倒计时
  chrome.runtime.getPlatformInfo(() => {});

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect();

// =========================================================================
// 💡对抗 Chrome MV3 休眠机制：终极不死保活法 (The MV3 Silver Bullet)
// =========================================================================
// 简单的 setInterval 容易被直接冻结。在里面调用 chrome.runtime.getPlatformInfo()
// 是目前唯一能 100% 强制刷新 MV3 Service Worker 死亡倒计时的官方后门方案！
setInterval(() => {
  chrome.runtime.getPlatformInfo(() => {
    if (nmPort) {
      safeSend({ type: 'PING', ts: Date.now() });
    } else {
      connect(); 
    }
  });
}, 20000); // 必须小于 Chrome 默认的 30 秒休眠阈值

// =========================================================================
// 监听系统底层网络状态变化，网线拔插瞬间无缝衔接
// =========================================================================
self.addEventListener('online', () => {
  console.log('[PB] 🌐 网络已恢复 (Online)！瞬间唤醒底层通道...');
  chrome.runtime.getPlatformInfo(() => {}); // 强行拉起活跃度
  if (!nmPort) connect();
});

self.addEventListener('offline', () => {
  console.log('[PB] 🚫 网络已断开 (Offline)...');
});

// 2. MV3 备用系统级保活：利用 Alarms API 双重兜底
if (chrome.alarms) {
  chrome.alarms.create('keepAliveAlarm', { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === 'keepAliveAlarm') {
       if (!nmPort) {
          console.log('[PB] ⏰ Alarm 唤醒: 检测到通道断开，执行紧急重连...');
          connect();
       } else {
          safeSend({ type: 'PING', ts: Date.now() });
       }
    }
  });
} else {
  console.warn('[PB] ⚠️ chrome.alarms API 未就绪，请确认 manifest.json 是否已配置 "alarms" 权限。');
}

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === 'status') sendResponse({ connected: !!nmPort });
  return true;
});