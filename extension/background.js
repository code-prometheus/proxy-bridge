// Proxy Bridge — Service Worker
// Receives HTTP requests from the Python proxy via Native Messaging,
// executes them with Chrome's fetch() API, and streams responses back.

const NATIVE_HOST_NAME = 'com.example.proxy_bridge';
const CHUNK_SIZE = 256 * 1024; // 256KB chunks for streaming

// ── Binary conversion utilities ──────────────────────────────────────────────

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

// ── Header filtering ─────────────────────────────────────────────────────────

function filterRequestHeaders(headers) {
    const drop = new Set([
        'host', 'connection', 'keep-alive', 'proxy-authorization',
        'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade'
    ]);
    const out = {};
    for (const [k, v] of Object.entries(headers || {})) {
        if (!drop.has(k.toLowerCase())) out[k] = v;
    }
    return out;
}

// ── Safe send to native host ──────────────────────────────────────────────────

function safeSend(msg) {
    try {
        if (nmPort) nmPort.postMessage(msg);
    } catch (_) {
        // Port disconnected; reconnect will handle it
    }
}

// ── Main request handler ─────────────────────────────────────────────────────

async function handleRequest(msg) {
    const { id, method, url, headers, _u8Body } = msg;
    try {
        const fetchOpts = {
            method,
            headers: filterRequestHeaders(headers),
            redirect: 'follow',
            credentials: 'omit',
            cache: 'no-store'
        };
        if (_u8Body) fetchOpts.body = _u8Body;

        const resp = await fetch(url, fetchOpts);

        // Build response headers — handle Set-Cookie correctly
        const respHeaders = {};
        const rawSetCookies = resp.headers.getSetCookie
            ? resp.headers.getSetCookie()
            : [];
        resp.headers.forEach((v, k) => {
            if (k.toLowerCase() !== 'set-cookie') respHeaders[k] = v;
        });
        if (rawSetCookies.length > 0) respHeaders['set-cookie'] = rawSetCookies;

        safeSend({
            type: 'response',
            id,
            status: resp.status,
            statusText: resp.statusText,
            headers: respHeaders
        });

        // Stream body in chunks
        if (resp.body) {
            const reader = resp.body.getReader();
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                if (value && value.length > 0) {
                    for (let i = 0; i < value.length; i += CHUNK_SIZE) {
                        const slice = value.subarray(i, i + CHUNK_SIZE);
                        safeSend({ type: 'chunk', id, data: uint8ToBase64(slice) });
                        await new Promise(r => setTimeout(r, 2)); // backpressure
                    }
                }
            }
        }
        safeSend({ type: 'end', id });
    } catch (err) {
        safeSend({ type: 'error', id, error: err.message || String(err) });
    }
}

// ── Request assembly from chunks ──────────────────────────────────────────────

const pendingRequests = {};

function handleRequestStart(msg) {
    const { id, method, url, headers } = msg;
    pendingRequests[id] = {
        id,
        method,
        url,
        headers,
        chunks: [],
        totalLen: 0
    };
}

function handleRequestChunk(msg) {
    const { id, data } = msg;
    const req = pendingRequests[id];
    if (!req) return;
    const chunk = base64ToUint8(data);
    req.chunks.push(chunk);
    req.totalLen += chunk.length;
}

function handleRequestEnd(msg) {
    const { id } = msg;
    const req = pendingRequests[id];
    if (!req) return;
    delete pendingRequests[id];

    let body = null;
    if (req.chunks.length > 0) {
        body = new Uint8Array(req.totalLen);
        let offset = 0;
        for (const chunk of req.chunks) {
            body.set(chunk, offset);
            offset += chunk.length;
        }
    }

    handleRequest({
        id: req.id,
        method: req.method,
        url: req.url,
        headers: req.headers,
        _u8Body: body
    });
}

// ── Message dispatch ──────────────────────────────────────────────────────────

function dispatchMessage(msg) {
    if (!msg || !msg.type) return;
    switch (msg.type) {
        case 'request_start':
            handleRequestStart(msg);
            break;
        case 'request_chunk':
            handleRequestChunk(msg);
            break;
        case 'request_end':
            handleRequestEnd(msg);
            break;
    }
}

// ── Native Messaging connection ───────────────────────────────────────────────

let nmPort = null;
let reconnectTimer = null;
let reconnectAttempts = 0;

function connect() {
    if (nmPort) {
        try { nmPort.disconnect(); } catch (_) {}
        nmPort = null;
    }

    try {
        nmPort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
        reconnectAttempts = 0; // reset on successful connection

        nmPort.onMessage.addListener((msg) => {
            dispatchMessage(msg);
        });

        nmPort.onDisconnect.addListener(() => {
            nmPort = null;
            scheduleReconnect();
        });
    } catch (_) {
        scheduleReconnect();
    }
}

function scheduleReconnect() {
    if (reconnectTimer) return; // already scheduled
    const delay = reconnectAttempts > 5 ? 3000 : 200;
    reconnectAttempts++;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        // Refresh idle timer via a harmless API call
        chrome.runtime.getPlatformInfo(() => {
            connect();
        });
    }, delay);
}

// ── Keep-alive ────────────────────────────────────────────────────────────────

// Platform info tick every 20s to prevent service worker suspension
setInterval(() => {
    chrome.runtime.getPlatformInfo(() => {});
}, 20000);

// Backup keep-alive via alarms (fires every minute)
chrome.alarms.create('keepalive', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === 'keepalive') {
        chrome.runtime.getPlatformInfo(() => {});
        if (nmPort) {
            try { nmPort.postMessage({ type: 'ping' }); } catch (_) {}
        }
    }
});

// ── Online/offline event listeners ────────────────────────────────────────────

self.addEventListener('online', () => {
    if (!nmPort) connect();
});

self.addEventListener('offline', () => {
    // Allow natural disconnect; reconnect happens when online fires
});

// ── Status query (for popup) ──────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
    if (req.action === 'status') {
        sendResponse({ connected: !!nmPort });
        return true;
    }
    return false;
});

// ── Initialize ────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect();
