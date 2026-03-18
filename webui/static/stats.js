window.__statsLoaded = true;

async function loadStats() {
  const token = await ensureAuth();
  void token;

  const hint = document.getElementById('stats-hint');
  if (hint) hint.textContent = '加载中...';

  try {
    let res = await fetch('/api/stats');
    if (res.status === 404) {
      res = await fetch('/api/stat');
    }
    if (!res.ok) {
      if (hint) hint.textContent = `加载失败 (${res.status})`;
      return;
    }
    const payload = await res.json();
    if (!payload.success) {
      if (hint) hint.textContent = payload.error || '加载失败';
      return;
    }
    const data = payload.data || {};

    document.getElementById('stats-total')?.textContent = data.total ?? 0;
    document.getElementById('stats-archived')?.textContent = data.archived ?? 0;
    document.getElementById('stats-unarchived')?.textContent = data.unarchived ?? 0;
    if (hint) hint.textContent = '';
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
  }
}

loadStats();
if (typeof bindLogout === 'function') {
  bindLogout();
}
