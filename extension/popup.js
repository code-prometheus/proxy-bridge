// Proxy Bridge v2.0 — Popup Script
const connectedView = document.getElementById('connected-view');
const setupView = document.getElementById('setup-view');
const extIdDisplay = document.getElementById('ext-id-display');
const copyIdBtn = document.getElementById('copy-id-btn');
const copyProxyBtn = document.getElementById('copy-proxy-btn');

const EXT_ID = chrome.runtime.id;
if (extIdDisplay) extIdDisplay.textContent = EXT_ID;

function showConnected() {
    setupView.style.display = 'none';
    connectedView.style.display = 'block';
}

function showSetup() {
    connectedView.style.display = 'none';
    setupView.style.display = 'block';
}

// Check Native Messaging connection status
chrome.runtime.sendMessage({action: 'status'}, (response) => {
    if (chrome.runtime.lastError || !response || !response.connected) {
        showSetup();
    } else {
        showConnected();
    }
});

// Button: Copy Extension ID
if (copyIdBtn) {
    copyIdBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(EXT_ID).then(() => {
            copyIdBtn.textContent = '✅ 已复制！粘贴到 bat 窗口';
            copyIdBtn.className = 'btn btn-copied';
            setTimeout(() => {
                copyIdBtn.textContent = '📋 复制扩展 ID';
                copyIdBtn.className = 'btn btn-primary';
            }, 2000);
        }).catch(() => {
            copyIdBtn.textContent = '❗ 复制失败，手动选择文字';
            setTimeout(() => {
                copyIdBtn.textContent = '📋 复制扩展 ID';
            }, 2000);
        });
    });
}

// Button: Copy Proxy URL
if (copyProxyBtn) {
    copyProxyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText('http://127.0.0.1:60130').then(() => {
            copyProxyBtn.textContent = '✅ 已复制代理地址';
            copyProxyBtn.className = 'btn btn-copied';
            setTimeout(() => {
                copyProxyBtn.textContent = '📋 复制代理地址';
                copyProxyBtn.className = 'btn btn-primary';
            }, 2000);
        }).catch(() => {
            copyProxyBtn.textContent = '❗ 复制失败';
            setTimeout(() => {
                copyProxyBtn.textContent = '📋 复制代理地址';
                copyProxyBtn.className = 'btn btn-primary';
            }, 2000);
        });
    });
}
