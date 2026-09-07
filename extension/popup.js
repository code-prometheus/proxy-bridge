// Proxy Bridge v2.0 — Popup Script
const connectedView = document.getElementById('connected-view');
const setupView = document.getElementById('setup-view');
const copyLocalBtn = document.getElementById('copy-local-btn');
const copyRemoteBtn = document.getElementById('copy-remote-btn');

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

// Button: Copy Local
if (copyLocalBtn) {
 copyLocalBtn.addEventListener('click', () => {
 navigator.clipboard.writeText('http://127.0.0.1:60130').then(() => {
 copyLocalBtn.textContent = '✅ 已复制';
 copyLocalBtn.className = 'btn btn-copied';
 setTimeout(() => {
 copyLocalBtn.textContent = '📋 复制代理地址';
 copyLocalBtn.className = 'btn btn-primary';
 }, 2000);
 });
 });
}

// Button: Copy Remote
if (copyRemoteBtn) {
 copyRemoteBtn.addEventListener('click', () => {
 navigator.clipboard.writeText('http://0.0.0.0:60130').then(() => {
 copyRemoteBtn.textContent = '✅ 已复制';
 copyRemoteBtn.className = 'btn btn-copied';
 setTimeout(() => {
 copyRemoteBtn.textContent = '📋 复制远程地址 (0.0.0.0:60130)';
 copyRemoteBtn.className = 'btn btn-primary';
 }, 2000);
 });
 });
}
