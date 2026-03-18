const FIELD_MAP = {
    "qq_id": "QQ号",
    "nickname": "昵称",
    "gender": "性别",
    "age": "年龄",
    "job": "职业",
    "location": "地区",
    "birthday": "生日",
    "zodiac": "生肖",
    "constellation": "星座"
};

const CATEGORY_MAP = {
    "personality_tags": "性格标签",
    "hobbies": "兴趣爱好",
    "favorite_foods": "美食偏好",
    "favorite_items": "心头好",
    "favorite_activities": "休闲活动",
    "dislikes": "厌恶禁忌"
};

let currentProfile = null;
let currentUserId = '';
let cachedUsers = [];
const LAST_PROFILE_USER_KEY = 'engram_last_profile_user';

async function loadUserOptions() {
    const headers = await getAuthHeaders();
    if (!headers) return;

    try {
        const res = await fetch('/api/users', { headers });
        const payload = await res.json();
        if (!payload.success) return;
        cachedUsers = payload.data?.items || [];
    } catch (e) {
        // ignore
    }
}

function renderUserSuggestions(filterText) {
    const list = document.getElementById('profile-user-suggest');
    if (!list) return;

    const normalized = (filterText || '').trim();
    if (!normalized) {
        list.innerHTML = '';
        list.classList.add('hidden');
        return;
    }

    const items = cachedUsers
        .filter((userId) => String(userId).includes(normalized))
        .slice(0, 8);

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
        item.addEventListener('mousedown', (event) => {
            event.preventDefault();
            const input = document.getElementById('profile-user');
            if (input) input.value = userId;
            list.classList.add('hidden');
            loadProfile(userId);
        });
        list.appendChild(item);
    });

    list.classList.remove('hidden');
}

async function loadProfile(explicitUserId = '') {
    const headers = await getAuthHeaders();
    if (!headers) return;

    const input = document.getElementById('profile-user');
    const userId = String(explicitUserId || input?.value.trim() || currentUserId || '').trim();
    const hint = document.getElementById('profile-hint');

    if (!userId) {
        hint.textContent = '请输入 user_id';
        return;
    }

    currentUserId = userId;
    if (input && explicitUserId) {
        input.value = '';
    }

    try {
        localStorage.setItem(LAST_PROFILE_USER_KEY, userId);
    } catch (e) {}

    try {
        hint.textContent = '同步画像中...';
        const res = await fetch(`/api/profile/${encodeURIComponent(userId)}`, { headers });
        const payload = await res.json();
        
        if (!payload.success) {
            hint.textContent = payload.error || '画像不存在';
            return;
        }

        currentProfile = payload.data || {};
        renderAll();
        hint.textContent = '';
    } catch (e) {
        hint.textContent = '网络请求失败';
        console.error(e);
    }
}

function renderAll() {
    if (!currentProfile) return;
    renderBasicInfo();
    renderTags();
    loadRenderedImage();
}

function renderBasicInfo() {
    const container = document.getElementById('info-display');
    const basic = currentProfile.basic_info || {};
    container.innerHTML = '';
    const ignoredFields = ['avatar_url'];
    
    // 仅显示 FIELD_MAP 中定义且非“未知”的字段
    Object.keys(FIELD_MAP).forEach(key => {
        const value = basic[key];
        if (!value || value === "未知") return;
        
        const label = FIELD_MAP[key];
        const item = document.createElement('div');
        item.className = 'info-item';
        item.innerHTML = `<span class="info-label">${label}：</span><span class="info-value">${value}</span>`;
        container.appendChild(item);
    });

    if (container.innerHTML === '') {
        container.innerHTML = '<div class="placeholder">暂无基础资料</div>';
    }
}

function renderTags() {
    const container = document.getElementById('tags-container');
    const attrs = currentProfile.attributes || {};
    const prefs = currentProfile.preferences || {};
    container.innerHTML = '';
    
    const tagData = {
        "personality_tags": attrs.personality_tags || [],
        "hobbies": attrs.hobbies || [],
        "favorite_foods": prefs.favorite_foods || [],
        "favorite_items": prefs.favorite_items || [],
        "favorite_activities": prefs.favorite_activities || [],
        "dislikes": prefs.dislikes || []
    };

    Object.entries(tagData).forEach(([catKey, list]) => {
        const group = document.createElement('div');
        group.className = 'tag-category-group';
        const title = document.createElement('div');
        title.className = 'tag-category-title';
        title.textContent = CATEGORY_MAP[catKey] || catKey;
        
        const chips = document.createElement('div');
        chips.className = 'tag-chips';
        chips.title = '双击空白处添加新标签';
        
        // 双击空白处添加标签逻辑
        chips.addEventListener('dblclick', (e) => {
            if (e.target !== chips && !e.target.classList.contains('placeholder')) return; 
            if (chips.querySelector('.inline-tag-input')) return;

            // 如果有占位符，先隐藏它以免挡路
            const placeholder = chips.querySelector('.placeholder');
            if (placeholder) placeholder.style.display = 'none';

            const input = document.createElement('input');
            input.className = 'form-input inline-tag-input';
            input.placeholder = '新标签...';
            input.style.cssText = 'width: 100px; height: 30px; font-size: 12px; border-radius: 999px; background: white; border: 1px solid #ccc; padding: 0 10px;';

            input.addEventListener('keydown', async (ke) => {
                if (ke.key === 'Enter') {
                    ke.preventDefault();
                    const val = input.value.trim();
                    if (val) {
                        await addTag(catKey, val);
                    } else {
                        renderTags();
                    }
                }
            });
            input.addEventListener('blur', () => {
                setTimeout(() => { if(input.parentNode) renderTags(); }, 150);
            });

            chips.appendChild(input);
            input.focus();
        });
        
        if (!list || list.length === 0) {
            const empty = document.createElement('span');
            empty.className = 'placeholder';
            empty.style.cssText = 'font-size: 12px; cursor: pointer;';
            empty.textContent = '暂无 (双击添加)';
            chips.appendChild(empty);
        } else {
            list.forEach(tag => {
                const chip = document.createElement('span');
                chip.className = 'tag-chip';
                chip.textContent = tag;
                chip.title = '双击删除';
                chip.addEventListener('dblclick', (e) => {
                    e.stopPropagation();
                    deleteTag(catKey, tag);
                });
                chips.appendChild(chip);
            });
        }
        group.appendChild(title);
        group.appendChild(chips);
        container.appendChild(group);
    });
}

async function addTag(category, value) {
    if (!currentProfile) return;
    
    if (["personality_tags", "hobbies"].includes(category)) {
        if (!currentProfile.attributes) currentProfile.attributes = {};
        if (!currentProfile.attributes[category]) currentProfile.attributes[category] = [];
        if (!currentProfile.attributes[category].includes(value)) {
            currentProfile.attributes[category].push(value);
        }
    } else {
        if (!currentProfile.preferences) currentProfile.preferences = {};
        if (!currentProfile.preferences[category]) currentProfile.preferences[category] = [];
        if (!currentProfile.preferences[category].includes(value)) {
            currentProfile.preferences[category].push(value);
        }
    }
    
    await saveProfile();
}

async function deleteTag(category, value) {
    const userId = currentUserId || document.getElementById('profile-user')?.value.trim();
    if (!currentProfile || !userId || !category || !value) return;

    const fieldPath = ["personality_tags", "hobbies"].includes(category)
        ? `attributes.${category}`
        : `preferences.${category}`;

    try {
        const headers = await getAuthHeaders({ 'Content-Type': 'application/json' });
        if (!headers) return;
        const res = await fetch(`/api/profile/${encodeURIComponent(userId)}/remove-item`, {
            method: 'POST',
            headers,
            body: JSON.stringify({ field_path: fieldPath, value })
        });
        const payload = await res.json();
        if (!payload.success) {
            alert('删除失败: ' + (payload.error || '未知错误'));
            return;
        }
        currentProfile = payload.data || currentProfile;
        renderAll();
    } catch (e) {
        console.error('Delete tag failed:', e);
        alert('删除失败');
    }
}

async function saveProfile() {
    const userId = currentUserId || document.getElementById('profile-user').value.trim();
    if (!userId || !currentProfile) return;

    try {
        const headers = await getAuthHeaders({ 'Content-Type': 'application/json' });
        if (!headers) return;
        const res = await fetch(`/api/profile/${encodeURIComponent(userId)}`, {
            method: 'POST',
            headers,
            body: JSON.stringify(currentProfile)
        });
        const payload = await res.json();
        if (payload.success) {
            // 直接更新本地 UI，不再次 fetch，减少延迟和失败风险
            renderAll();
        } else {
            alert('保存失败: ' + payload.error);
        }
    } catch (e) {
        console.error('Save failed:', e);
    }
}

async function loadRenderedImage() {
    const userId = currentUserId || document.getElementById('profile-user').value.trim();
    if (!userId) return;
    const canvas = document.getElementById('profile-canvas');
    const headers = await getAuthHeaders();
    if (!headers) return;

    const imgUrl = `/api/profile/${encodeURIComponent(userId)}/render?t=${Date.now()}`;

    try {
        const res = await fetch(imgUrl, { headers });
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const objectUrl = URL.createObjectURL(blob);
        const img = new Image();
        img.src = objectUrl;
        img.onload = () => {
            canvas.innerHTML = '';
            canvas.appendChild(img);
            canvas.onclick = () => {
                const overlay = document.createElement('div');
                overlay.className = 'preview-overlay';
                const fullImg = new Image();
                fullImg.src = objectUrl;
                overlay.appendChild(fullImg);
                overlay.onclick = () => {
                    document.body.removeChild(overlay);
                };
                document.body.appendChild(overlay);
            };
        };
        img.onerror = () => {
            URL.revokeObjectURL(objectUrl);
            canvas.innerHTML = '<div class="placeholder">渲染图片未就绪</div>';
        };
    } catch (e) {
        canvas.innerHTML = '<div class="placeholder">渲染图片未就绪</div>';
    }
}

// 绑定初始事件
document.addEventListener('DOMContentLoaded', async () => {
    document.getElementById('profile-load')?.addEventListener('click', () => {
        const input = document.getElementById('profile-user');
        const value = input?.value.trim() || '';
        loadProfile(value);
    });

    const profileInput = document.getElementById('profile-user');
    if (profileInput) {
        try {
            currentUserId = localStorage.getItem(LAST_PROFILE_USER_KEY) || '';
            profileInput.value = '';
        } catch (e) {}

        profileInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                loadProfile();
            }
        });
        profileInput.addEventListener('input', (event) => {
            renderUserSuggestions(event.target.value);
        });
        profileInput.addEventListener('blur', () => {
            setTimeout(() => {
                document.getElementById('profile-user-suggest')?.classList.add('hidden');
            }, 150);
        });
    }

    await loadUserOptions();

    document.getElementById('profile-clear')?.addEventListener('click', async () => {
        const userId = currentUserId || document.getElementById('profile-user').value.trim();
        if (!userId || !confirm('确定要清除该用户的所有画像和标签吗？')) return;
        const headers = await getAuthHeaders();
        if (!headers) return;
        const res = await fetch(`/api/profile/${encodeURIComponent(userId)}`, { method: 'DELETE', headers });
        const payload = await res.json();
        if (payload.success) {
            currentProfile = null;
            renderAll();
        }
    });

    bindLogout();
    if (currentUserId) {
        await loadProfile(currentUserId);
    }
});
