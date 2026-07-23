const notice = document.querySelector('#admin-notice');
let users = [];
let currentVaultId = null;
let currentVaultName = '';
let vaultSelectionToken = 0;
let membersRequestToken = 0;
let membersLoadedVaultId = null;
let membershipRequestToken = 0;
let transferRequestToken = 0;
let quotaRequestToken = 0;
let quotaSaveRequestToken = 0;
let quotaLoadedVaultId = null;
let noticeTimer;
let noticeHideTimer;
const csrfCookieName = 'frostvault_csrf';
const mutatingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
let authMethod = null;

function readCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) return decodeURIComponent(cookie.slice(prefix.length));
  }
  return '';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

function apiErrorMessage(data, status) {
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map(item => item.msg || 'Invalid value').join('; ');
  }
  if (typeof data.error === 'string') return data.error;
  return status >= 500
    ? `Internal server error (HTTP ${status})`
    : `Operation failed (HTTP ${status})`;
}

function formatQuotaValue(value, unit) {
  return value === null || value === undefined ? 'Unlimited' : `${escapeHtml(value)} ${unit}`;
}

function quotaInputNames() {
  return [
    'storage_soft_limit_bytes', 'storage_hard_limit_bytes',
    'concurrency_soft_limit', 'concurrency_hard_limit',
    'restore_30d_soft_limit_bytes', 'restore_30d_hard_limit_bytes',
  ];
}

function readQuotaValue(form, name) {
  const raw = String(form.get(name) ?? '').trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw)) throw new Error(`${name} must be a nonnegative integer.`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) throw new Error(`${name} is too large.`);
  return value;
}

function quotaStatusMarkup(data) {
  const evaluation = data.evaluation;
  if (!evaluation || evaluation.state === 'unevaluated'
      || typeof evaluation.allowed !== 'boolean' || !Array.isArray(evaluation.decisions)) {
    return `<span class="quota-state-item unavailable">${evaluation?.state === 'unevaluated'
      ? 'Quota state not evaluated.' : 'Quota state unavailable.'}</span>`;
  }
  if (!evaluation.decisions.length) {
    return evaluation.allowed
      ? '<span class="quota-state-item ok">No active warnings or blocks reported.</span>'
      : '<span class="quota-state-item unavailable">Quota state unavailable.</span>';
  }
  return evaluation.decisions.map(decision => {
    const severity = decision.severity === 'block' ? 'Block' : 'Warning';
    return `<span class="quota-state-item ${escapeHtml(decision.severity || 'warning')}">${severity}: ${escapeHtml(decision.code || 'quota decision')}</span>`;
  }).join('');
}

function show(message, error = false) {
  window.clearTimeout(noticeTimer);
  window.clearTimeout(noticeHideTimer);
  notice.querySelector('.notice-message').textContent = message;
  notice.classList.toggle('error', error);
  notice.setAttribute('role', error ? 'alert' : 'status');
  notice.classList.remove('hidden', 'is-visible', 'is-leaving');
  void notice.offsetWidth;
  notice.classList.add('is-visible');
  noticeTimer = window.setTimeout(dismissNotice, 4200);
}

function dismissNotice() {
  window.clearTimeout(noticeTimer);
  notice.classList.remove('is-visible');
  notice.classList.add('is-leaving');
  noticeHideTimer = window.setTimeout(() => {
    notice.classList.add('hidden');
    notice.classList.remove('is-leaving');
  }, 280);
}

notice.querySelector('.notice-close').addEventListener('click', dismissNotice);

async function stepUpReauthentication() {
  if (authMethod === 'oidc') {
    const returnTo = window.location.pathname + window.location.search;
    window.location.href = `/auth/oidc/reauth?return_to=${encodeURIComponent(returnTo)}`;
    return false;
  }
  const password = window.prompt('Confirm your password to continue with this sensitive action.');
  if (!password) return false;
  const response = await fetch('/api/reauth', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': readCookie(csrfCookieName)},
    body: JSON.stringify({password}),
  });
  if (!response.ok) {
    show('Reauthentication failed.', true);
    return false;
  }
  return true;
}

async function api(url, options = {}, allowReauthRetry = true) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if (mutatingMethods.has(method)) headers['X-CSRF-Token'] = readCookie(csrfCookieName);
  const response = await fetch(url, {...options, headers});
  const body = await response.text();
  let data = {};
  if (body) {
    try {
      data = JSON.parse(body);
    } catch (_) {
      if (response.ok) throw new Error(`Invalid response from the server (HTTP ${response.status})`);
    }
  }
  if (response.status === 403 && data.error === 'reauth_required' && allowReauthRetry) {
    const reauthenticated = await stepUpReauthentication();
    if (reauthenticated) return api(url, options, false);
    throw new Error('Reauthentication required for this action.');
  }
  if (!response.ok) {
    throw new Error(apiErrorMessage(data, response.status));
  }
  return data;
}

async function loadUsers() {
  const data = await api('/api/admin/users');
  users = data.items;
  document.querySelector('#owner-select').innerHTML = users.filter(u => u.active).map(u =>
    `<option value="${u.id}">${escapeHtml(u.display_name)} (${escapeHtml(u.username)})</option>`
  ).join('');
  document.querySelector('#users-list').innerHTML = users.map(u => `
    <div class="admin-row">
      <div><strong>${escapeHtml(u.display_name)}</strong><small>@${escapeHtml(u.username)} · ${u.vault_count} vaults${u.is_admin ? ' · administrator' : ''}</small></div>
      <div class="row-actions">
        <span class="badge ${u.active ? 'both' : 'missing'}">${u.active ? 'Active' : 'Disabled'}</span>
        <button class="secondary" data-reset-user="${u.id}">New password</button>
        <button class="secondary" data-toggle-user="${u.id}" data-active="${u.active}">${u.active ? 'Deactivate' : 'Reactivate'}</button>
      </div>
    </div>`).join('');
}

async function loadVaults(isCurrent = () => true) {
  const data = await api('/api/admin/vaults');
  if (!isCurrent()) return;
  document.querySelector('#vaults-list').innerHTML = data.items.map(v => `
    <div class="admin-row">
      <div><strong>${escapeHtml(v.name)}</strong><small>${escapeHtml(v.slug)} · ${v.member_count} users · ${escapeHtml(v.s3_prefix)}</small></div>
      <div class="row-actions"><span class="badge ${v.enabled ? 'both' : 'missing'}">${v.enabled ? 'Active' : 'Disabled'}</span><button class="secondary" data-members="${v.id}" data-name="${escapeHtml(v.name)}">Manage access</button></div>
    </div>`).join('');
}

function populateTransferTargets(members) {
  const target = document.querySelector('#transfer-owner-user');
  const eligible = members.filter(member => member.active && member.role !== 'owner');
  target.innerHTML = eligible.map(member =>
    `<option value="${escapeHtml(member.id)}">${escapeHtml(member.display_name)} (@${escapeHtml(member.username)})</option>`
  ).join('');
  target.disabled = eligible.length === 0;
  document.querySelector('#transfer-owner-form button[type="submit"]').disabled = eligible.length === 0;
}

function resetQuotaDisplay() {
  quotaLoadedVaultId = null;
  document.querySelector('#quota-form').reset();
  document.querySelector('#quota-load-state').textContent = 'Loading quota status…';
  document.querySelector('#admin-quota-usage').textContent = '';
  document.querySelector('#quota-save').disabled = true;
}

function renderAdminQuota(data) {
  const limits = data.limits || {};
  for (const name of quotaInputNames()) {
    const input = document.querySelector(`#quota-form [name="${name}"]`);
    input.value = limits[name] === null || limits[name] === undefined ? '' : String(limits[name]);
  }
  const usage = data.usage || {};
  document.querySelector('#admin-quota-usage').innerHTML = `
    <span>Current storage: <strong>${formatQuotaValue(usage.storage_bytes, 'bytes')}</strong></span>
    <span>Active jobs: <strong>${formatQuotaValue(usage.concurrency, 'jobs')}</strong></span>
    <span>Restore usage (30 days): <strong>${formatQuotaValue(usage.restore_30d_bytes, 'bytes')}</strong></span>
    ${quotaStatusMarkup(data)}
  `;
  document.querySelector('#quota-load-state').textContent = 'Quota status loaded.';
}

async function loadAdminQuota(vaultId, selectionToken) {
  const selectedVaultId = Number(vaultId);
  // A refresh can keep the same vault selection while replacing its quota view;
  // invalidate an in-flight save before loading the newer snapshot.
  ++quotaSaveRequestToken;
  const requestToken = ++quotaRequestToken;
  resetQuotaDisplay();
  const isCurrent = () => requestToken === quotaRequestToken
    && currentVaultId === selectedVaultId && selectionToken === vaultSelectionToken;
  try {
    const data = await api(`/api/admin/vaults/${selectedVaultId}/quotas`);
    if (!isCurrent()) return;
    renderAdminQuota(data);
    quotaLoadedVaultId = selectedVaultId;
    document.querySelector('#quota-save').disabled = false;
  } catch (error) {
    if (!isCurrent()) return;
    document.querySelector('#quota-load-state').textContent = error.message;
  }
}

async function openMembers(vaultId, vaultName, refresh = false) {
  const selectedVaultId = Number(vaultId);
  const selectedVaultName = vaultName;
  if (!refresh) vaultSelectionToken += 1;
  const selectionToken = vaultSelectionToken;
  const requestToken = ++membersRequestToken;
  currentVaultId = selectedVaultId;
  currentVaultName = selectedVaultName;
  membersLoadedVaultId = null;
  document.querySelector('#members-title').textContent = `Access · ${selectedVaultName}`;
  document.querySelector('#member-user').innerHTML = users.filter(u => u.active).map(u =>
    `<option value="${u.id}">${escapeHtml(u.display_name)} (@${escapeHtml(u.username)})</option>`
  ).join('');
  // Do not leave the previous vault's members actionable while this request is pending.
  document.querySelector('#members-list').innerHTML = '';
  populateTransferTargets([]);
  void loadAdminQuota(selectedVaultId, selectionToken);
  try {
    const data = await api(`/api/admin/vaults/${selectedVaultId}/members`);
    if (requestToken !== membersRequestToken || currentVaultId !== selectedVaultId || selectionToken !== vaultSelectionToken) return;
    document.querySelector('#members-list').innerHTML = data.items.map(member => `
      <div class="admin-row"><div><strong>${escapeHtml(member.display_name)}</strong><small>@${escapeHtml(member.username)} · ${escapeHtml(member.role === 'owner' ? 'primary owner' : member.role)}</small></div>${member.role === 'owner' ? '' : `<button class="secondary" data-remove-member="${escapeHtml(member.id)}">Remove</button>`}</div>
    `).join('') || '<p class="subtitle">No users assigned.</p>';
    populateTransferTargets(data.items);
    membersLoadedVaultId = selectedVaultId;
    const dialog = document.querySelector('#members-dialog');
    if (!dialog.open) dialog.showModal();
  } catch (error) {
    if (requestToken === membersRequestToken && currentVaultId === selectedVaultId && selectionToken === vaultSelectionToken) throw error;
  }
}

document.querySelector('#user-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  try {
    await api('/api/admin/users', {method:'POST', body:JSON.stringify({
      display_name: form.get('display_name'), username: form.get('username'),
      password: form.get('password'), is_admin: form.get('is_admin') === 'on'
    })});
    event.currentTarget.reset(); show('User created'); await loadUsers();
  } catch (error) { show(error.message, true); }
});

document.querySelector('#vault-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries()); payload.owner_user_id = Number(payload.owner_user_id);
  try {
    await api('/api/admin/vaults', {method:'POST', body:JSON.stringify(payload)});
    event.currentTarget.reset(); show('Private vault created'); await loadVaults();
  } catch (error) { show(error.message, true); }
});

document.querySelector('#quota-form').addEventListener('submit', async event => {
  event.preventDefault();
  const vaultId = currentVaultId;
  const selectionToken = vaultSelectionToken;
  if (vaultId === null || quotaLoadedVaultId !== vaultId) {
    show('Wait for the selected vault quotas to finish loading.', true);
    return;
  }
  const form = new FormData(event.currentTarget);
  const payload = {};
  try {
    for (const name of quotaInputNames()) payload[name] = readQuotaValue(form, name);
    const pairs = [
      ['storage_soft_limit_bytes', 'storage_hard_limit_bytes', 'storage'],
      ['concurrency_soft_limit', 'concurrency_hard_limit', 'concurrency'],
      ['restore_30d_soft_limit_bytes', 'restore_30d_hard_limit_bytes', 'restore 30-day'],
    ];
    for (const [softName, hardName, label] of pairs) {
      if (payload[softName] !== null && payload[hardName] !== null && payload[softName] > payload[hardName]) {
        throw new Error(`Soft ${label} limit cannot exceed the hard limit.`);
      }
    }
    payload.reason = String(form.get('reason') || '').trim();
    if (!payload.reason) throw new Error('Enter a reason for this quota change.');
  } catch (error) {
    show(error.message, true);
    return;
  }
  const requestToken = ++quotaSaveRequestToken;
  const isCurrentRequest = () => requestToken === quotaSaveRequestToken
    && currentVaultId === vaultId && selectionToken === vaultSelectionToken;
  const button = document.querySelector('#quota-save');
  button.disabled = true;
  try {
    const data = await api(`/api/admin/vaults/${vaultId}/quotas`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    if (!isCurrentRequest()) return;
    renderAdminQuota(data);
    quotaLoadedVaultId = vaultId;
    event.currentTarget.querySelector('[name="reason"]').value = '';
    show('Vault quotas updated');
  } catch (error) {
    if (isCurrentRequest()) show(error.message, true);
  } finally {
    if (isCurrentRequest()) button.disabled = false;
  }
});

document.querySelector('#users-list').addEventListener('click', async event => {
  const toggle = event.target.closest('[data-toggle-user]');
  const reset = event.target.closest('[data-reset-user]');
  try {
    if (toggle) {
      const active = toggle.dataset.active !== 'true';
      await api(`/api/admin/users/${toggle.dataset.toggleUser}`, {method:'PATCH', body:JSON.stringify({active})});
      show(active ? 'User reactivated' : 'User deactivated'); await loadUsers();
    }
    if (reset) {
      const password = window.prompt('Enter a new password containing at least 12 characters');
      if (!password) return;
      await api(`/api/admin/users/${reset.dataset.resetUser}`, {method:'PATCH', body:JSON.stringify({password})});
      show('Password updated');
    }
  } catch (error) { show(error.message, true); }
});

document.querySelector('#vaults-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-members]');
  if (!button) return;
  try { await openMembers(button.dataset.members, button.dataset.name); }
  catch (error) { show(error.message, true); }
});

document.querySelector('#member-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const vaultId = currentVaultId;
  const vaultName = currentVaultName;
  const selectionToken = vaultSelectionToken;
  const requestToken = ++membershipRequestToken;
  const isCurrentRequest = () => requestToken === membershipRequestToken
    && currentVaultId === vaultId && selectionToken === vaultSelectionToken;
  const reason = String(form.get('reason') || '').trim();
  if (!reason) { show('Enter a reason for this administrator override.', true); return; }
  try {
    await api(`/api/admin/vaults/${vaultId}/members`, {method:'POST', body:JSON.stringify({user_id:Number(form.get('user_id')), role:form.get('role'), reason})});
    if (!isCurrentRequest()) return;
    show('Access updated'); await openMembers(vaultId, vaultName, true);
    if (!isCurrentRequest()) return;
    await loadVaults(isCurrentRequest);
  } catch (error) {
    if (isCurrentRequest()) show(error.message, true);
  }
});

document.querySelector('#transfer-owner-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const vaultId = currentVaultId;
  const vaultName = currentVaultName;
  const selectionToken = vaultSelectionToken;
  const requestToken = ++transferRequestToken;
  const isCurrentRequest = () => requestToken === transferRequestToken
    && currentVaultId === vaultId && selectionToken === vaultSelectionToken;
  if (vaultId === null || membersLoadedVaultId !== vaultId) {
    show('Wait for the current vault members to finish loading.', true);
    return;
  }
  const reason = String(form.get('reason') || '').trim();
  if (!reason) { show('Enter a reason for this ownership transfer.', true); return; }
  if (!form.get('confirmation')) { show('Confirm the ownership transfer before continuing.', true); return; }
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await api(`/api/admin/vaults/${vaultId}/transfer-owner`, {method:'POST', body:JSON.stringify({new_owner_user_id:Number(form.get('new_owner_user_id')), reason})});
    if (!isCurrentRequest()) return;
    show('Ownership transferred'); event.currentTarget.reset();
    await openMembers(vaultId, vaultName, true);
    if (!isCurrentRequest()) return;
    await loadVaults(isCurrentRequest);
  } catch (error) {
    if (isCurrentRequest()) show(error.message, true);
  }
  finally {
    if (isCurrentRequest()) button.disabled = false;
  }
});

document.querySelector('#members-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-remove-member]');
  if (!button || !window.confirm('Remove access for this user?')) return;
  const vaultId = currentVaultId;
  const vaultName = currentVaultName;
  const selectionToken = vaultSelectionToken;
  const requestToken = ++membershipRequestToken;
  const isCurrentRequest = () => requestToken === membershipRequestToken
    && currentVaultId === vaultId && selectionToken === vaultSelectionToken;
  const reason = window.prompt('Enter a reason for removing this access.');
  const trimmedReason = String(reason || '').trim();
  if (!trimmedReason) return;
  try {
    const query = new URLSearchParams({reason: trimmedReason});
    await api(`/api/admin/vaults/${vaultId}/members/${button.dataset.removeMember}?${query}`, {method:'DELETE'});
    if (!isCurrentRequest()) return;
    show('Access removed'); await loadVaults(isCurrentRequest);
    if (!isCurrentRequest()) return;
    await openMembers(vaultId, vaultName, true);
  } catch (error) {
    if (isCurrentRequest()) show(error.message, true);
  }
});

async function loadIdentity() {
  try {
    const data = await api('/api/me');
    authMethod = data.auth_method || null;
  } catch (_) {
    // Non-fatal: reauth step-up defaults to a password prompt.
  }
}

loadIdentity();
Promise.all([loadUsers(), loadVaults()]).catch(error => show(error.message, true));
