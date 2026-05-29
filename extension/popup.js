const statusBox = document.getElementById('status-box');
const guideBox = document.getElementById('guide-box');
const copyBtn = document.getElementById('copy-id-btn');

// 获取当前扩展 ID
const extId = chrome.runtime.id;

chrome.runtime.sendMessage({ action: 'status' }, (response) => {
  if (chrome.runtime.lastError) {
    showError("Service Worker 通信失败");
    return;
  }

  if (response && response.connected) {
    statusBox.className = 'status success';
    statusBox.innerHTML = '🟢 已连接，代理运行中';
    guideBox.style.display = 'none';
  } else {
    showError("未检测到本地服务");
  }
});

function showError(msg) {
  statusBox.className = 'status error';
  statusBox.innerHTML = `🔴 ${msg}`;
  guideBox.style.display = 'block';
}

copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(extId).then(() => {
    copyBtn.innerText = '已复制! 去粘贴到 Bat 脚本';
    copyBtn.style.background = '#34a853';
  });
});