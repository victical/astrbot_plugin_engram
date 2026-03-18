async function updatePassword(event) {
  event.preventDefault();
  const token = localStorage.getItem('engram_token');
  if (!token) {
    window.location.href = '/';
    return;
  }

  const hint = document.getElementById('password-hint');
  const oldPassword = document.getElementById('old-password').value.trim();
  const newPassword = document.getElementById('new-password').value.trim();

  if (newPassword.length < 8) {
    hint.textContent = '新密码至少 8 位';
    return;
  }

  hint.textContent = '保存中...';

  try {
    const res = await fetch('/api/password', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
    });
    const payload = await res.json();
    if (!payload.success) {
      hint.textContent = payload.detail || payload.error || '保存失败';
      return;
    }
    hint.textContent = '密码已更新，正在进入...';
    window.location.href = '/static/dashboard.html';
  } catch (e) {
    hint.textContent = '保存失败';
  }
}

const form = document.getElementById('password-form');
form?.addEventListener('submit', updatePassword);
