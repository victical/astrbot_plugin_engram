let currentMemoryId = null;
let currentUserId = null;

const deleteBtn = document.getElementById('detail-delete');
const actionHint = document.getElementById('detail-action-hint');

async function loadMemoryDetail() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  const params = new URLSearchParams(window.location.search);
  const memoryId = params.get('memory_id');
  const userId = params.get('user_id');
  const hint = document.getElementById('detail-hint');

  if (!memoryId || !userId) {
    hint.textContent = '缺少 memory_id 或 user_id';
    return;
  }

  currentMemoryId = memoryId;
  currentUserId = userId;

  actionHint.textContent = '';
  try {
    const res = await fetch(`/api/memories/${encodeURIComponent(memoryId)}?user_id=${encodeURIComponent(userId)}`, { headers });
    const payload = await res.json();
    if (!payload.success) {
      hint.textContent = payload.error || '加载失败';
      return;
    }

    const memory = payload.data?.memory || {};
    const messages = payload.data?.messages || [];
    const aiName = payload.data?.ai_name || '助手';

    document.getElementById('detail-id').textContent = memory.index_id?.slice(0, 8) || '-';
    document.getElementById('detail-user').textContent = memory.user_id || '-';
    document.getElementById('detail-source').textContent = memory.source_type || '-';
    document.getElementById('detail-time').textContent = memory.created_at?.replace('T', ' ').slice(0, 16) || '-';
    document.getElementById('detail-summary').textContent = memory.summary || '-';

    const stream = document.getElementById('detail-stream');
    stream.innerHTML = '';
    if (!messages.length) {
      hint.textContent = '暂无原始对话';
      return;
    }

    const visibleMessages = messages.filter((msg) => String(msg.content || '').trim());
    if (!visibleMessages.length) {
      hint.textContent = '原始对话均为空或被过滤';
      return;
    }

    visibleMessages.forEach((msg) => {
      const bubble = document.createElement('div');
      bubble.className = `chat-bubble${msg.role === 'assistant' ? ' assistant' : ''}`;
      const time = (msg.timestamp || '').replace('T', ' ').slice(0, 16);
      bubble.innerHTML = `
        <p class="chat-role">${msg.role === 'assistant' ? aiName : (msg.user_name || '用户')}</p>
        <p>${msg.content || ''}</p>
        <span class="chat-time">${time}</span>
      `;
      stream.appendChild(bubble);
    });
    hint.textContent = '';
  } catch (e) {
    hint.textContent = '加载失败';
  }
}

deleteBtn?.addEventListener('click', async () => {
  const headers = await getAuthHeaders();
  if (!headers) return;
  if (!currentMemoryId || !currentUserId) return;
  if (!confirm('确定删除该记忆？')) return;
  actionHint.textContent = '正在删除...';
  const res = await fetch(`/api/memories/${encodeURIComponent(currentMemoryId)}?user_id=${encodeURIComponent(currentUserId)}`, {
    method: 'DELETE',
    headers
  });
  const payload = await res.json();
  actionHint.textContent = payload.success ? '已删除' : payload.error || '删除失败';
  if (payload.success) {
    setTimeout(() => {
      window.location.href = '/static/memories.html';
    }, 800);
  }
});

loadMemoryDetail();
bindLogout();
