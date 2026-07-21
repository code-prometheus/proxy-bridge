const statusBox = document.getElementById('status-box');
const statusText = document.getElementById('status-text');
const guideBox = document.getElementById('guide-box');
const copyBtn = document.getElementById('copy-id-btn');
const hsCheckbox = document.getElementById('hs-checkbox');
const saveIndicator = document.getElementById('save-indicator');

const extId = chrome.runtime.id;
const API_URL = 'http://127.0.0.1:60130/proxy-api/models';

function setStatus(online) {
  if (online) {
    statusBox.className = 'status online';
    statusText.innerText = '已连接，通道运行中';
    guideBox.style.display = 'none';
    hsCheckbox.disabled = false;
  } else {
    statusBox.className = 'status offline';
    statusText.innerText = '未检测到核心服务';
    guideBox.style.display = 'block';
    hsCheckbox.disabled = true;
  }
}

// 检查 Native 通道连接状态
chrome.runtime.sendMessage({ action: 'status' }, (response) => {
  if (chrome.runtime.lastError) {
    setStatus(false);
    return;
  }
  if (response && response.connected) {
    setStatus(true);
    loadConfig();
  } else {
    setStatus(false);
  }
});

// 复制扩展 ID
copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(extId).then(() => {
    copyBtn.innerText = '✅ 已复制！';
    copyBtn.style.background = 'linear-gradient(135deg, #22c55e, #16a34a)';
  });
});

// 加载握手开关状态
function loadConfig() {
  fetch(API_URL)
    .then(res => res.json())
    .then(data => {
      if (data.enable_handshake !== undefined) {
        hsCheckbox.checked = data.enable_handshake;
      }
      hsCheckbox.disabled = false;
    })
    .catch(() => {});
}

// 握手开关事件
hsCheckbox.addEventListener('change', (e) => {
  const enabled = e.target.checked;
  hsCheckbox.disabled = true;
  saveIndicator.style.color = '#94a3b8';
  saveIndicator.innerText = '保存中...';

  fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enable_handshake: enabled })
  })
    .then(res => res.json())
    .then(data => {
      hsCheckbox.disabled = false;
      if (data.status === 'success') {
        saveIndicator.style.color = '#22c55e';
        saveIndicator.innerText = '✅ 已保存';
      } else {
        saveIndicator.style.color = '#ef4444';
        saveIndicator.innerText = '❌ 保存失败';
        hsCheckbox.checked = !enabled;
      }
      setTimeout(() => { saveIndicator.innerText = ''; }, 2000);
    })
    .catch(() => {
      hsCheckbox.disabled = false;
      hsCheckbox.checked = !enabled;
      saveIndicator.style.color = '#ef4444';
      saveIndicator.innerText = '❌ 网络错误';
      setTimeout(() => { saveIndicator.innerText = ''; }, 2000);
    });
});
