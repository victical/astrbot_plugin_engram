async function loadDashboard() {
  let token = null;
  if (typeof ensureAuth === 'function') {
    token = await ensureAuth();
  }

  const hint = document.getElementById('dashboard-hint');
  if (hint) hint.textContent = '加载中...';

  const headers = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  try {
    let payload = null;
    const res = await fetch('/api/stats/overview', { headers });
    if (res.ok) {
      payload = await res.json();
    }

    if (!payload || !payload.success) {
      const fallback = await fetch('/api/stats', { headers });
      if (!fallback.ok) {
        if (hint) hint.textContent = `加载失败 (HTTP ${fallback.status})`;
        return;
      }
      const fallbackPayload = await fallback.json();
      if (!fallbackPayload.success) {
        if (hint) hint.textContent = fallbackPayload.error || '加载失败';
        return;
      }
      payload = { success: true, data: { private: fallbackPayload.data, group: null, group_enabled: false } };
    }

    const data = payload.data || {};
    const privateStats = data.private || {};

    const statTotal = document.getElementById('stat-total');
    if (statTotal) statTotal.textContent = (privateStats.total ?? 0).toLocaleString();

    const statUsers = document.getElementById('stat-users');
    if (statUsers) statUsers.textContent = (privateStats.user_count ?? 0).toLocaleString();

    const statArchived = document.getElementById('stat-archived');
    if (statArchived) statArchived.textContent = (privateStats.archived ?? 0).toLocaleString();

    const unarchivedEl = document.getElementById('stat-unarchived');
    if (unarchivedEl) {
      unarchivedEl.textContent = `未归档 ${(privateStats.unarchived ?? 0).toLocaleString()}`;
    }

    const statIndex = document.getElementById('stat-index');
    if (statIndex) statIndex.textContent = (privateStats.memory_index_count ?? 0).toLocaleString();

    const dbEl = document.getElementById('stat-db');
    if (dbEl) {
      dbEl.textContent = privateStats.db_path ? `数据库 ${privateStats.db_path}` : '数据库 --';
    }

    const groupStats = data.group || {};
    const groupEnabled = data.group_enabled !== false;
    const groupTotal = document.getElementById('group-stat-total');
    if (groupTotal) groupTotal.textContent = groupEnabled ? (groupStats.total ?? 0).toLocaleString() : '--';
    const groupUsers = document.getElementById('group-stat-users');
    if (groupUsers) groupUsers.textContent = groupEnabled ? ((groupStats.group_count ?? groupStats.user_count ?? 0)).toLocaleString() : '--';
    const groupArchived = document.getElementById('group-stat-archived');
    if (groupArchived) groupArchived.textContent = groupEnabled ? (groupStats.archived ?? 0).toLocaleString() : '--';
    const groupUnarchivedEl = document.getElementById('group-stat-unarchived');
    if (groupUnarchivedEl) {
      groupUnarchivedEl.textContent = groupEnabled
        ? `未归档 ${(groupStats.unarchived ?? 0).toLocaleString()}`
        : '未归档 --';
    }
    const groupIndex = document.getElementById('group-stat-index');
    if (groupIndex) groupIndex.textContent = groupEnabled ? (groupStats.memory_index_count ?? 0).toLocaleString() : '--';
    const groupDbEl = document.getElementById('group-stat-db');
    if (groupDbEl) {
      groupDbEl.textContent = groupEnabled && groupStats.db_path ? `数据库 ${groupStats.db_path}` : '数据库 --';
    }

    if (hint) hint.textContent = '';

    document.getElementById('stat-group-memories')?.addEventListener('click', () => {
      window.location.href = '/static/group-memories.html';
    }, { once: true });

    // 初始化图表
    if (window.Chart && data.history) {
        renderChart(data.history);
    }

  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    console.error(e);
  }
}

function renderChart(history) {
    const ctx = document.getElementById('statsChart')?.getContext('2d');
    if (!ctx) return;

    // 如果已经有图表实例，销毁它
    if (window.myStatsChart) {
        window.myStatsChart.destroy();
    }

    const labels = history.map(item => item.date);
    const privateData = history.map(item => item.private_count);
    const groupData = history.map(item => item.group_count);

    window.myStatsChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: '私聊',
                    data: privateData,
                    borderColor: '#37423d',
                    backgroundColor: 'rgba(55, 66, 61, 0.1)',
                    borderWidth: 2,
                    tension: 0.3,
                    fill: true
                },
                {
                    label: '群聊',
                    data: groupData,
                    borderColor: '#a86666',
                    backgroundColor: 'rgba(168, 102, 102, 0.1)',
                    borderWidth: 2,
                    tension: 0.3,
                    fill: true
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        boxWidth: 12,
                        font: { size: 12, family: 'inherit' },
                        color: '#5c6460'
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(0, 0, 0, 0.05)' },
                    ticks: { color: '#5c6460' }
                },
                x: {
                    grid: { display: false },
                    ticks: { color: '#5c6460' }
                }
            }
        }
    });
}

async function loadActivities() {
  let token = null;
  if (typeof ensureAuth === 'function') {
    token = await ensureAuth();
  }
  const headers = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const hint = document.getElementById('activity-hint');
  const list = document.getElementById('activity-list');
  if (!list) return;

  try {
    const res = await fetch('/api/activities', { headers });
    if (!res.ok) {
      if (hint) hint.textContent = '加载失败';
      return;
    }
    const payload = await res.json();
    if (!payload.success) {
      if (hint) hint.textContent = payload.error || '加载失败';
      return;
    }
    const items = payload.data || [];
    list.innerHTML = '';
    if (!items.length) {
      if (hint) hint.textContent = '暂无动态';
      return;
    }
    if (hint) hint.textContent = '';
    items.forEach((item) => {
      const li = document.createElement('li');
      li.className = 'activity-item ' + (item.source === 'group' ? 'group' : 'private');
      const title = document.createElement('span');
      title.className = 'activity-title';
      title.textContent = item.title || '-';
      const meta = document.createElement('span');
      meta.className = 'activity-meta';
      const source = item.source === 'group' ? '群聊' : '私聊';
      const time = item.ts || '';
      meta.textContent = `${time} · ${source}`;
      li.appendChild(title);
      li.appendChild(meta);
      list.appendChild(li);
    });
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
  }
}

function bindLocalLogout() {
  const logoutBtn = document.getElementById('logout');
  if (!logoutBtn) return;
  logoutBtn.addEventListener('click', async () => {
    const token = localStorage.getItem('engram_token');
    if (token) {
      try {
        await fetch('/api/logout', {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` }
        });
      } catch (e) {
        // ignore
      }
    }
    localStorage.removeItem('engram_token');
    window.location.href = '/';
  });
}

// 确保在页面加载后执行
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    loadDashboard();
    loadActivities();
  });
} else {
  loadDashboard();
  loadActivities();
}

if (typeof bindLogout === 'function') {
  bindLogout();
} else {
  bindLocalLogout();
}
