// Proxy Bridge v2.0 — Popup Script
const statusDot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const statusSub = document.getElementById('status-sub');
const extIdEl = document.getElementById('ext-id');
const copyIdBtn = document.getElementById('copy-id-btn');
const copyProxyBtn = document.getElementById('copy-proxy-btn');

const EXT_ID = chrome.runtime.id;
if (extIdEl) extIdEl.textContent = EXT_ID.slice(0, 16) + '...';

function setStatus(online) {
    if (!statusDot || !statusText || !statusSub) return;
    if (online) {
        statusDot.className = 'dot online';
        statusText.textContent = 'Connected — Proxy Active';
        statusSub.textContent = 'All local apps can use 127.0.0.1:60130';
    } else {
        statusDot.className = 'dot offline';
        statusText.textContent = 'Disconnected';
        statusSub.textContent = 'Run python entry.py or reload extension';
    }
}

// Check Native Messaging connection status
chrome.runtime.sendMessage({action: 'status'}, (response) => {
    if (chrome.runtime.lastError) {
        setStatus(false);
        return;
    }
    setStatus(response && response.connected);
});

// Copy Extension ID
if (copyIdBtn) {
    copyIdBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(EXT_ID).then(() => {
            copyIdBtn.textContent = '✅ Copied!';
            copyIdBtn.style.background = '#16a34a';
            setTimeout(() => {
                copyIdBtn.textContent = '📋 Copy Extension ID';
                copyIdBtn.style.background = '';
            }, 1800);
        }).catch(() => {
            copyIdBtn.textContent = '❗ Copy failed';
            setTimeout(() => {
                copyIdBtn.textContent = '📋 Copy Extension ID';
            }, 1500);
        });
    });
}

// Copy Proxy URL
if (copyProxyBtn) {
    copyProxyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText('http://127.0.0.1:60130').then(() => {
            copyProxyBtn.textContent = '✅ Proxy URL Copied!';
            setTimeout(() => {
                copyProxyBtn.textContent = '🔗 Copy Proxy URL';
            }, 1800);
        }).catch(() => {
            copyProxyBtn.textContent = '❗ Copy failed';
            setTimeout(() => {
                copyProxyBtn.textContent = '🔗 Copy Proxy URL';
            }, 1500);
        });
    });
}
