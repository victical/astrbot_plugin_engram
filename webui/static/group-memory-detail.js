let currentMemoryId = '';
let currentGroupId = '';
let currentMemberId = '';

async function loadGroupMemoryDetail() {
  const headers = await getAuthHeaders();
  if (!headers) return;

  const params = new URLSearchParams(window.location.search);
  const memoryId = params.get('memory_id');
  const groupId = params.get('group_id');
  const memberId = '';
  const hint = document.getElementById('detail-hint');

  if (!memoryId || !groupId) {
    hint.textContent = '缺少 memory_id 或 group_id';
    return;
  }

  currentMemoryId = memoryId;
  currentGroupId = groupId;
  currentMemberId = memberId;
  document.getElementById('detail-action-hint').textContent = '';

  try {
    const res = await fetch(`/api/group-memories/${encodeURIComponent(memoryId)}?group_id=${encodeURIComponent(groupId)}`, { headers });
    const payload = await res.json();
    if (!payload.success) {
      hint.textContent = payload.error || payload.detail || '加载失败';
      return;
    }

    const memory = payload.data?.memory || {};
    const messages = payload.data?.messages || [];
    const aiName = payload.data?.ai_name || '助手';
    const participants = memory.participants || [];

    document.getElementById('detail-id').textContent = memory.index_id?.slice(0, 8) || '-';
    document.getElementById('detail-group').textContent = memory.group_id || groupId || '-';
    document.getElementById('detail-member').textContent = memory.member_id || currentMemberId || '多人';
    document.getElementById('detail-source').textContent = memory.source_type || '-';
    document.getElementById('detail-time').textContent = memory.created_at?.replace('T', ' ').slice(0, 16) || '-';
    document.getElementById('detail-summary').textContent = memory.summary || '-';
    document.getElementById('detail-participants').textContent = participants.length
      ? `参与成员：${participants.map((item) => item.member_name || item.member_id).join('、')}`
      : '';

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
      const roleName = msg.role === 'assistant'
        ? aiName
        : (msg.user_name || msg.member_id || '群成员');
      bubble.innerHTML = `
        <p class="chat-role">${roleName}</p>
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

document.getElementById('detail-delete')?.addEventListener('click', async () => {
  const headers = await getAuthHeaders();
  if (!headers) return;
  if (!currentMemoryId || !currentGroupId) return;
  if (!confirm('确定删除该群聊记忆？')) return;

  const actionHint = document.getElementById('detail-action-hint');
  actionHint.textContent = '正在删除...';
  const res = await fetch(`/api/group-memories/${encodeURIComponent(currentMemoryId)}?group_id=${encodeURIComponent(currentGroupId)}`, {
    method: 'DELETE',
    headers,
  });
  const payload = await res.json();
  actionHint.textContent = payload.success ? '已删除' : payload.error || payload.detail || '删除失败';
  if (payload.success) {
    setTimeout(() => {
      const backUrl = `/static/group-memories.html?group_id=${encodeURIComponent(currentGroupId)}${currentMemberId ? `&member_id=${encodeURIComponent(currentMemberId)}` : ''}`;
      window.location.href = backUrl;
    }, 800);
  }
});

loadGroupMemoryDetail();
bindLogout();
