let currentPage = 1;
let cachedGroups = [];

async function loadGroupOptions() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  try {
    const res = await fetch('/api/groups', { headers });
    const payload = await res.json();
    if (!payload.success) return;
    cachedGroups = payload.data?.items || [];
    renderGroupSuggestions('');
  } catch (e) {
    // ignore
  }
}

function renderGroupSuggestions(filterText) {
  const list = document.getElementById('group-suggest');
  if (!list) return;

  const normalized = (filterText || '').trim();
  if (!normalized) {
    list.classList.add('hidden');
    return;
  }

  const items = cachedGroups.filter((groupId) => String(groupId).includes(normalized)).slice(0, 8);
  list.innerHTML = '';

  if (!items.length) {
    list.classList.add('hidden');
    return;
  }

  items.forEach((groupId) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'suggest-item';
    item.textContent = groupId;
    item.addEventListener('mousedown', () => {
      document.getElementById('group-id').value = groupId;
      list.classList.add('hidden');
    });
    list.appendChild(item);
  });

  list.classList.remove('hidden');
}

async function loadGroupMemories() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  const groupId = document.getElementById('group-id')?.value.trim();
  const memberId = document.getElementById('member-id')?.value.trim();
  const keyword = document.getElementById('group-keyword')?.value.trim();
  const date = document.getElementById('group-date')?.value;
  const pageSize = document.getElementById('page-size')?.value || '20';
  const tbody = document.getElementById('group-memory-body');
  const hint = document.getElementById('group-hint');
  const pageInfo = document.getElementById('page-info');

  const params = new URLSearchParams({
    page: String(currentPage),
    page_size: pageSize,
  });
  if (groupId) params.set('group_id', groupId);
  if (memberId) params.set('member_id', memberId);

  try {
    hint.textContent = '加载中...';
    hint.classList.remove('error');

    let payload = null;
    if (keyword) {
      const res = await fetch('/api/group-memories/search', {
        method: 'POST',
        headers: {
          ...headers,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          group_id: groupId,
          member_id: memberId,
          query: keyword,
          limit: 200,
        }),
      });
      payload = await res.json();
    } else {
      const res = await fetch(`/api/group-memories?${params.toString()}`, { headers });
      payload = await res.json();
    }

    if (!payload.success) {
      hint.textContent = payload.error || payload.detail || '加载失败';
      hint.classList.add('error');
      return;
    }

    const items = payload.data?.items || [];
    const filtered = items.filter((item) => {
      if (!date) return true;
      return String(item.created_at || '').startsWith(date);
    });

    tbody.innerHTML = '';
    if (!filtered.length) {
      hint.textContent = '暂无数据';
      pageInfo.textContent = `第 ${currentPage} 页`;
      document.getElementById('page-next').disabled = true;
      document.getElementById('page-prev').disabled = currentPage <= 1;
      updateBatchDeleteVisible();
      return;
    }

    filtered.forEach((item) => {
      const row = document.createElement('tr');
      const resolvedGroupId = item.group_id || groupId || item.user_id || '';
      const link = `/static/group-memory-detail.html?memory_id=${encodeURIComponent(item.id)}&group_id=${encodeURIComponent(resolvedGroupId)}&member_id=${encodeURIComponent(item.member_id || memberId || '')}`;
      row.innerHTML = `
        <td><input type="checkbox" class="memory-check" data-id="${item.id}" data-group="${resolvedGroupId}" /></td>
        <td><code title="${item.id}">${item.id?.slice(0, 8) || '-'}</code></td>
        <td><div class="summary-cell">${item.summary || '-'}</div></td>
        <td><span class="user-id-label">${resolvedGroupId || '-'}</span></td>
        <td><span class="badge" style="text-transform:none; padding: 2px 8px;">${item.source_type || '-'}</span></td>
        <td style="white-space: nowrap;">${item.created_at ? item.created_at.replace('T', ' ').slice(0, 16) : '-'}</td>
        <td class="action-cell">
          <a class="text-link" href="${link}">查看</a>
          <button class="text-link danger" data-id="${item.id}" data-group="${resolvedGroupId}">删除</button>
        </td>
      `;
      tbody.appendChild(row);
    });

    document.querySelectorAll('.memory-check').forEach((cb) => {
      cb.addEventListener('change', updateBatchDeleteVisible);
    });
    document.getElementById('check-all').checked = false;
    updateBatchDeleteVisible();

    const total = payload.data?.total || filtered.length;
    pageInfo.textContent = `第 ${currentPage} 页 · 共 ${total} 条`;
    hint.textContent = '';
    document.getElementById('page-next').disabled = keyword ? true : !Boolean(payload.data?.has_more);
    document.getElementById('page-prev').disabled = currentPage <= 1;
  } catch (e) {
    hint.textContent = '加载失败';
    hint.classList.add('error');
  }
}

async function handleDelete(event) {
  const target = event.target;
  if (!target.classList.contains('danger')) return;

  const headers = await getAuthHeaders();
  if (!headers) return;

  const memoryId = target.getAttribute('data-id');
  const groupId = target.getAttribute('data-group');
  if (!memoryId || !groupId) return;
  if (!confirm('确定删除该群聊记忆？')) return;

  await fetch(`/api/group-memories/${encodeURIComponent(memoryId)}?group_id=${encodeURIComponent(groupId)}`, {
    method: 'DELETE',
    headers,
  });
  loadGroupMemories();
}

async function handleBatchDelete() {
  const selected = Array.from(document.querySelectorAll('.memory-check:checked')).map((cb) => ({
    id: cb.getAttribute('data-id'),
    group: cb.getAttribute('data-group'),
  }));

  if (!selected.length) return;
  if (!confirm(`确定要删除选中的 ${selected.length} 条群聊记忆吗？`)) return;

  const headers = await getAuthHeaders();
  if (!headers) return;

  const btn = document.getElementById('batch-delete');
  const originalText = btn.textContent;
  btn.disabled = true;

  for (let i = 0; i < selected.length; i += 1) {
    const item = selected[i];
    btn.textContent = `正在删除 (${i + 1}/${selected.length})...`;
    try {
      await fetch(`/api/group-memories/${encodeURIComponent(item.id)}?group_id=${encodeURIComponent(item.group)}`, {
        method: 'DELETE',
        headers,
      });
    } catch (e) {
      console.error('Delete failed:', item.id, e);
    }
  }

  btn.textContent = originalText;
  btn.disabled = false;
  loadGroupMemories();
}

function updateBatchDeleteVisible() {
  const checked = document.querySelectorAll('.memory-check:checked');
  const btn = document.getElementById('batch-delete');
  if (!btn) return;

  if (checked.length > 0) {
    btn.classList.remove('hidden');
    btn.textContent = `批量删除 (${checked.length})`;
  } else {
    btn.classList.add('hidden');
  }
}

const urlParams = new URLSearchParams(window.location.search);
const presetGroup = urlParams.get('group_id');
const presetMember = urlParams.get('member_id');
const presetKeyword = urlParams.get('keyword');
if (presetGroup) document.getElementById('group-id').value = presetGroup;
if (presetKeyword) document.getElementById('group-keyword').value = presetKeyword;

document.getElementById('group-search')?.addEventListener('click', () => {
  currentPage = 1;
  loadGroupMemories();
});

document.getElementById('group-id')?.addEventListener('input', (event) => {
  renderGroupSuggestions(event.target.value);
});

document.getElementById('group-id')?.addEventListener('focus', (event) => {
  renderGroupSuggestions(event.target.value);
});

document.getElementById('group-id')?.addEventListener('blur', () => {
  setTimeout(() => {
    document.getElementById('group-suggest')?.classList.add('hidden');
  }, 150);
});

document.getElementById('page-prev')?.addEventListener('click', () => {
  if (currentPage > 1) {
    currentPage -= 1;
    loadGroupMemories();
  }
});

document.getElementById('page-next')?.addEventListener('click', () => {
  currentPage += 1;
  loadGroupMemories();
});

document.getElementById('page-size')?.addEventListener('change', () => {
  currentPage = 1;
  loadGroupMemories();
});

document.getElementById('group-memory-body')?.addEventListener('click', handleDelete);
document.getElementById('check-all')?.addEventListener('change', (event) => {
  const checked = event.target.checked;
  document.querySelectorAll('.memory-check').forEach((cb) => {
    cb.checked = checked;
  });
  updateBatchDeleteVisible();
});
document.getElementById('batch-delete')?.addEventListener('click', handleBatchDelete);

loadGroupOptions();
loadGroupMemories();
bindLogout();
oupMemories();
bindLogout();
