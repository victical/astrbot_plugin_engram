let currentPage = 1;
let cachedUsers = [];

async function loadUserOptions() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  try {
    const res = await fetch('/api/users', { headers });
    const payload = await res.json();
    if (!payload.success) return;
    cachedUsers = payload.data?.items || [];
    renderUserSuggestions('');
  } catch (e) {
    // ignore
  }
}

function renderUserSuggestions(filterText) {
  const list = document.getElementById('user-suggest');
  if (!list) return;
  const normalized = (filterText || '').trim();
  
  // 如果输入为空，直接隐藏列表并返回
  if (!normalized) {
    list.classList.add('hidden');
    return;
  }

  const items = cachedUsers.filter((userId) => {
    return String(userId).includes(normalized);
  }).slice(0, 8);

  list.innerHTML = '';
  if (!items.length) {
    list.classList.add('hidden');
    return;
  }

  items.forEach((userId) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'suggest-item';
    item.textContent = userId;
    item.addEventListener('mousedown', () => {
      document.getElementById('memory-user').value = userId;
      list.classList.add('hidden');
    });
    list.appendChild(item);
  });
  list.classList.remove('hidden');
}

async function loadMemories() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  const userInput = document.getElementById('memory-user');
  const keywordInput = document.getElementById('memory-keyword');
  const dateInput = document.getElementById('memory-date');
  const tbody = document.getElementById('memory-body');
  const hint = document.getElementById('memory-hint');
  const pageInfo = document.getElementById('page-info');
  const pageSize = document.getElementById('page-size');

  const userId = userInput?.value.trim();
  const keyword = keywordInput?.value.trim();
  const date = dateInput?.value;

  const params = new URLSearchParams();
  if (userId) params.set('user_id', userId);
  params.set('page', String(currentPage));
  params.set('page_size', pageSize?.value || '20');

  try {
    hint.textContent = '加载中...';
    hint.classList.remove('error');
    let payload = null;

    if (keyword) {
      if (!userId) {
        hint.textContent = '关键词搜索需要提供 user_id';
        hint.classList.add('error');
        return;
      }
      const res = await fetch('/api/memories/search', {
        method: 'POST',
        headers: {
          ...headers,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          user_id: userId,
          query: keyword,
          limit: 200
        })
      });
      payload = await res.json();
    } else {
      const res = await fetch(`/api/memories?${params.toString()}`, { headers });
      payload = await res.json();
    }

    if (!payload.success) {
      hint.textContent = payload.error || '加载失败';
      hint.classList.add('error');
      return;
    }

    const items = payload.data?.items || [];
    tbody.innerHTML = '';

    const filtered = items.filter((item) => {
      if (!date) return true;
      return String(item.created_at || '').startsWith(date);
    });

    if (!filtered.length) {
      hint.textContent = '暂无数据';
      pageInfo.textContent = `第 ${currentPage} 页`;
      return;
    }

    hint.classList.remove('error');

    filtered.forEach((item) => {
      const row = document.createElement('tr');
      const link = `/static/memory-detail.html?memory_id=${encodeURIComponent(item.id)}&user_id=${encodeURIComponent(item.user_id || '')}`;
      row.innerHTML = `
        <td><input type="checkbox" class="memory-check" data-id="${item.id}" data-user="${item.user_id}" /></td>
        <td><code title="${item.id}">${item.id?.slice(0, 8) || '-'}</code></td>
        <td><div class="summary-cell">${item.summary || '-'}</div></td>
        <td><span class="user-id-label" data-user-id="${item.user_id}">${item.user_id || '-'}</span></td>
        <td><span class="badge" style="text-transform:none; padding: 2px 8px;">${item.source_type || '-'}</span></td>
        <td style="white-space: nowrap;">${item.created_at ? item.created_at.replace('T', ' ').slice(0, 16) : '-'}</td>
        <td class="action-cell">
          <a class="text-link" href="${link}">查看</a>
          <button class="text-link danger" data-id="${item.id}" data-user="${item.user_id}">删除</button>
        </td>
      `;
      tbody.appendChild(row);
    });

    // 绑定勾选框事件
    document.querySelectorAll('.memory-check').forEach(cb => {
        cb.addEventListener('change', updateBatchDeleteVisible);
    });
    document.getElementById('check-all').checked = false;
    updateBatchDeleteVisible();

    const total = payload.data?.total || filtered.length;
    pageInfo.textContent = `第 ${currentPage} 页 · 共 ${total} 条`;
    hint.textContent = '';

    const hasMore = Boolean(payload.data?.has_more);
    document.getElementById('page-next').disabled = keyword ? true : !hasMore;
    document.getElementById('page-prev').disabled = currentPage <= 1;
  } catch (e) {
    hint.textContent = '加载失败';
  }
}

async function handleDelete(event) {
  const target = event.target;
  if (!target.classList.contains('danger')) return;
  const headers = await getAuthHeaders();
  if (!headers) return;

  const memoryId = target.getAttribute('data-id');
  const userId = target.getAttribute('data-user');
  if (!memoryId || !userId) return;
  if (!confirm('确定删除该记忆？')) return;

  await fetch(`/api/memories/${encodeURIComponent(memoryId)}?user_id=${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    headers
  });
  loadMemories();
}

const searchBtn = document.getElementById('memory-search');
searchBtn?.addEventListener('click', () => {
  currentPage = 1;
  loadMemories();
});

const urlParams = new URLSearchParams(window.location.search);
const presetUser = urlParams.get('user_id');
const presetKeyword = urlParams.get('keyword');
if (presetUser) document.getElementById('memory-user').value = presetUser;
if (presetKeyword) document.getElementById('memory-keyword').value = presetKeyword;

const userInput = document.getElementById('memory-user');
userInput?.addEventListener('input', (event) => {
  renderUserSuggestions(event.target.value);
});
userInput?.addEventListener('focus', (event) => {
  renderUserSuggestions(event.target.value);
});
userInput?.addEventListener('blur', () => {
  setTimeout(() => {
    document.getElementById('user-suggest')?.classList.add('hidden');
  }, 150);
});

document.getElementById('page-prev')?.addEventListener('click', () => {
  if (currentPage > 1) {
    currentPage -= 1;
    loadMemories();
  }
});

document.getElementById('page-next')?.addEventListener('click', () => {
  currentPage += 1;
  loadMemories();
});

document.getElementById('page-size')?.addEventListener('change', () => {
  currentPage = 1;
  loadMemories();
});

document.getElementById('memory-body')?.addEventListener('click', handleDelete);

async function handleBatchDelete() {
    const selected = Array.from(document.querySelectorAll('.memory-check:checked')).map(cb => ({
        id: cb.getAttribute('data-id'),
        user: cb.getAttribute('data-user')
    }));

    if (selected.length === 0) return;
    if (!confirm(`确定要删除选中的 ${selected.length} 条记忆吗？此操作不可撤销（除非使用 /mem_undo 指令）。`)) return;

    const headers = await getAuthHeaders();
    if (!headers) return;

    const btn = document.getElementById('batch-delete');
    const originalText = btn.textContent;
    btn.disabled = true;
    
    let successCount = 0;
    for (let i = 0; i < selected.length; i++) {
        const item = selected[i];
        btn.textContent = `正在删除 (${i + 1}/${selected.length})...`;
        try {
            await fetch(`/api/memories/${encodeURIComponent(item.id)}?user_id=${encodeURIComponent(item.user)}`, {
                method: 'DELETE',
                headers
            });
            successCount++;
        } catch (e) {
            console.error('Delete failed:', item.id, e);
        }
    }

    btn.textContent = originalText;
    btn.disabled = false;
    loadMemories();
}

function updateBatchDeleteVisible() {
    const checked = document.querySelectorAll('.memory-check:checked');
    const btn = document.getElementById('batch-delete');
    if (checked.length > 0) {
        btn.classList.remove('hidden');
        btn.textContent = `批量删除 (${checked.length})`;
    } else {
        btn.classList.add('hidden');
    }
}

document.getElementById('check-all')?.addEventListener('change', (e) => {
    const checked = e.target.checked;
    document.querySelectorAll('.memory-check').forEach(cb => {
        cb.checked = checked;
    });
    updateBatchDeleteVisible();
});

document.getElementById('batch-delete')?.addEventListener('click', handleBatchDelete);

loadUserOptions();
loadMemories();
bindLogout();
