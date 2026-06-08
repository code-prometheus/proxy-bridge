const statusBox = document.getElementById('status-box');
const guideBox = document.getElementById('guide-box');
const copyBtn = document.getElementById('copy-id-btn');

// LLM 切换器相关 DOM
const llmSection = document.getElementById('llm-section');
const llmSelect = document.getElementById('llm-select');
const llmStatus = document.getElementById('llm-status');
const LLM_API_URL = 'http://127.0.0.1:60130/proxy-api/models';

// 获取当前扩展 ID
const extId = chrome.runtime.id;

// ==========================================
// 1. 检查底层 Native 通道连接状态
// ==========================================
chrome.runtime.sendMessage({ action: 'status' }, (response) => {
  if (chrome.runtime.lastError) {
    showError("Service Worker 通信失败");
    return;
  }

  if (response && response.connected) {
    // 代理桥接成功
    statusBox.className = 'status success';
    statusBox.innerHTML = '🟢 已连接，Native 通道运行中';
    guideBox.style.display = 'none';
    
    // 激活下方的 LLM 选择区域并请求 Python 接口
    llmSection.style.opacity = '1';
    llmSection.style.pointerEvents = 'auto';
    llmSelect.disabled = false;
    loadLLMModels();
  } else {
    // 代理未连接，显示 Bat 安装指南
    showError("未检测到本地核心服务");
  }
});

function showError(msg) {
  statusBox.className = 'status error';
  statusBox.innerHTML = `🔴 ${msg}`;
  guideBox.style.display = 'block';
}

// 复制扩展 ID 逻辑
copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(extId).then(() => {
    copyBtn.innerText = '已复制! 去粘贴到 Bat 脚本';
    copyBtn.style.background = '#34a853';
  });
});

// ==========================================
// 2. 加载与切换 LLM 动态模型配置
// ==========================================
function loadLLMModels() {
  llmSelect.innerHTML = '<option value="">正在读取配置...</option>';
  llmStatus.style.color = '#666';
  llmStatus.innerText = '获取列表中...';
  
  fetch(LLM_API_URL)
    .then(res => res.json())
    .then(data => {
      llmSelect.innerHTML = ''; 
      
      if (!data.models || data.models.length === 0) {
        llmSelect.innerHTML = '<option value="">未在 settings.json 找到模型</option>';
        llmSelect.disabled = true;
        llmStatus.style.color = '#c5221f';
        llmStatus.innerText = '请按照模板配置 settings';
        return;
      }

      // 渲染下拉菜单
      data.models.forEach(model => {
        const opt = document.createElement('option');
        opt.value = model;
        opt.textContent = model;
        if (model === data.active_llm) {
          opt.selected = true; // 默认选中 Python 当前生效的模型
        }
        llmSelect.appendChild(opt);
      });
      
      llmStatus.style.color = '#137333';
      llmStatus.innerText = '✅ 配置读取成功';
      setTimeout(() => { llmStatus.innerText = ''; }, 2000);
    })
    .catch(err => {
      llmSelect.innerHTML = '<option value="">本地 API 访问失败</option>';
      llmSelect.disabled = true;
      llmStatus.style.color = '#c5221f';
      llmStatus.innerText = '⚠️ 请确保 Python 代理已启动';
    });
}

// 监听用户切换模型操作
llmSelect.addEventListener('change', (e) => {
  const newModel = e.target.value;
  if (!newModel) return;

  // 锁定菜单，防止并发修改
  llmSelect.disabled = true;
  llmStatus.style.color = '#666';
  llmStatus.innerText = '正在保存并应用...';

  fetch(LLM_API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active_llm: newModel })
  })
  .then(res => res.json())
  .then(data => {
    llmSelect.disabled = false;
    if (data.status === 'success') {
      llmStatus.style.color = '#137333';
      llmStatus.innerText = '✅ 切换成功，已实时生效';
    } else {
      llmStatus.style.color = '#c5221f';
      llmStatus.innerText = '❌ 切换失败: ' + data.msg;
    }
    
    // 2秒后清除成功提示
    setTimeout(() => { 
      if(llmStatus.innerText.includes('成功')) {
        llmStatus.innerText = ''; 
      }
    }, 2000);
  })
  .catch(err => {
    llmSelect.disabled = false;
    llmStatus.style.color = '#c5221f';
    llmStatus.innerText = '❌ 网络请求错误';
  });
});