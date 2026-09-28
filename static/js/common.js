/* 共用小工具：API 呼叫、提示訊息、輪詢、偏好設定 */
(function () {
  function store(key, val) {
    try {
      if (val === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, val);
    } catch (e) { /* 私密瀏覽等情況拿不到 localStorage，就不記住偏好 */ }
    return null;
  }

  async function api(url, opts) {
    opts = opts || {};
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new Error('連不上展示網站，請確認電腦上的黑色視窗還開著、手機和電腦在同一個網路。');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || ('伺服器錯誤（' + res.status + '）'));
    return data;
  }

  function post(url, body) {
    return api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }

  let toastTimer = null;
  function toast(msg, ms) {
    let t = document.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.add('hidden'); }, ms || 2600);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmt(n) { return Number(n || 0).toLocaleString('en-US'); }

  function poll(fn, ms) {
    let busy = false;
    async function tick() {
      if (busy || document.hidden) return;
      busy = true;
      try { await fn(); } catch (e) { console.warn('更新失敗，下一輪再試：', e); }
      busy = false;
    }
    tick();
    // 分頁切回前景時立刻更新一次（在背景時為了省電會暫停更新）
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
    return setInterval(tick, ms || 3000);
  }

  function busy(btn, on, text) {
    if (!btn) return;
    if (on) {
      btn.dataset.label = btn.innerHTML;
      btn.disabled = true;
      btn.classList.add('busy');
      btn.innerHTML = '<span class="spinner"></span>' + esc(text || '處理中…');
    } else {
      btn.disabled = false;
      btn.classList.remove('busy');
      if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
    }
  }

  // 字體放大（長輩友善）
  function initFont() {
    if (store('big-font') === '1') document.body.classList.add('big');
    document.querySelectorAll('[data-font-toggle]').forEach(function (b) {
      b.addEventListener('click', function () {
        const on = document.body.classList.toggle('big');
        store('big-font', on ? '1' : '0');
        toast(on ? '已放大字體' : '已恢復一般字體', 1400);
      });
    });
  }

  // 員工端右上角的 AI 模式標示
  async function initAiPill() {
    const pill = document.querySelector('[data-ai-pill]');
    if (!pill) return;
    try {
      const s = await api('/api/ai/status');
      pill.className = 'ai-pill ' + s.mode;
      pill.textContent = 'AI：' + s.label;
      pill.title = s.detail + (s.last_error ? '\n最近一次錯誤：' + s.last_error : '');
    } catch (e) { pill.textContent = 'AI：狀態未知'; }
  }

  // 目前頁面在導覽列上反白
  function initNav() {
    const path = location.pathname;
    document.querySelectorAll('.s-nav a').forEach(function (a) {
      const href = a.getAttribute('href');
      if ((href === '/' && path === '/') || (href !== '/' && path.indexOf(href) === 0)) a.classList.add('on');
    });
  }

  document.addEventListener('DOMContentLoaded', function () { initFont(); initAiPill(); initNav(); });

  // 車主頁共用：指定技師、加選／取消自選項目（成功後重新整理頁面，費用與完工時間會跟著更新）
  function bindCustomerWidgets(vid) {
    document.querySelectorAll('[data-tech]').forEach(function (b) {
      b.addEventListener('click', async function () {
        busy(b, true, '處理中…');
        try {
          await post('/api/visit/' + vid + '/technician', { technician_id: b.dataset.tech, by: 'customer' });
          toast(b.dataset.tech ? '已指定技師' : '已取消指定');
          setTimeout(function () { location.reload(); }, 500);
        } catch (e) { toast(e.message, 3500); busy(b, false); }
      });
    });
    document.querySelectorAll('[data-opt]').forEach(function (b) {
      b.addEventListener('click', async function () {
        const on = b.dataset.on === '1';
        busy(b, true, on ? '加選中…' : '取消中…');
        try {
          await post('/api/visit/' + vid + '/optional', { code: b.dataset.opt, on: on });
          toast(on ? '已加選，費用與完工時間已更新' : '已取消這個項目');
          setTimeout(function () { location.reload(); }, 500);
        } catch (e) { toast(e.message, 3500); busy(b, false); }
      });
    });
  }

  window.App = { api: api, post: post, toast: toast, esc: esc, fmt: fmt, poll: poll, busy: busy, store: store,
                 bindCustomerWidgets: bindCustomerWidgets };
})();
