window.__authLoaded = true;

async function ensureAuth() {
  const token = localStorage.getItem('engram_token');
  if (!token) {
    if (!window.location.pathname.endsWith('/index.html') && window.location.pathname !== '/') {
      window.location.href = '/';
    }
    return null;
  }
  return token;
}

async function getAuthHeaders(extra = {}) {
  const token = await ensureAuth();
  if (!token) return null;
  return { ...extra, Authorization: `Bearer ${token}` };
}

async function bindLogout() {
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
    localStorage.removeItem('engram_force_change');
    window.location.href = '/';
  });
}
