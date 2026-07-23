const notice = document.querySelector('#access-notice');
const csrfCookieName = 'frostvault_csrf';
const mutatingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
let authMethod = null;
let lookupUser = null;
let operationPolicy = null;
let noticeTimer;
let noticeHideTimer;

function readCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) return decodeURIComponent(cookie.slice(prefix.length));
  }
  return '';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[character]));
}

function roleLabel(role) {
  return {owner: 'primary owner', operator: 'operator', viewer: 'viewer'}[role] || 'not a member';
}

function apiErrorMessage(data, status) {
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map(item => item.msg || 'Invalid value').join('; ');
  if (typeof data.error === 'string') return data.error;
  return status >= 500
    ? `Internal server error (HTTP ${status})`
    : `Operation failed (HTTP ${status})`;
}

function formatQuotaValue(value, unit, unknown = false) {
  if (unknown) return 'Unknown';
  return value === null || value === undefined ? 'Unlimited' : `${escapeHtml(value)} ${unit}`;
}

function renderOwnerQuota(data) {
  const limits = data.limits || {};
  const usage = data.usage || {};
  document.querySelector('#owner-quota-limits').innerHTML = `
    <h3>Effective limits</h3>
    <dl>
      <div><dt>Storage</dt><dd>${formatQuotaValue(limits.storage_soft_limit_bytes, 'bytes')} soft · ${formatQuotaValue(limits.storage_hard_limit_bytes, 'bytes')} hard</dd></div>
      <div><dt>Concurrency</dt><dd>${formatQuotaValue(limits.concurrency_soft_limit, 'jobs')} soft · ${formatQuotaValue(limits.concurrency_hard_limit, 'jobs')} hard</dd></div>
      <div><dt>Restore usage (30 days)</dt><dd>${formatQuotaValue(limits.restore_30d_soft_limit_bytes, 'bytes')} soft · ${formatQuotaValue(limits.restore_30d_hard_limit_bytes, 'bytes')} hard</dd></div>
    </dl>
  `;
  document.querySelector('#owner-quota-usage').innerHTML = `
    <h3>Current usage</h3>
    <dl>
      <div><dt>Storage</dt><dd>${formatQuotaValue(usage.storage_bytes, 'bytes', usage.storage_unknown)}</dd></div>
      <div><dt>Active jobs</dt><dd>${formatQuotaValue(usage.concurrency, 'jobs')}</dd></div>
      <div><dt>Restore usage (30 days)</dt><dd>${formatQuotaValue(usage.restore_30d_bytes, 'bytes', usage.restore_request_unknown)}</dd></div>
    </dl>
  `;
  const evaluation = data.evaluation;
  const state = document.querySelector('#owner-quota-state');
  if (!evaluation || evaluation.state === 'unevaluated'
      || typeof evaluation.allowed !== 'boolean' || !Array.isArray(evaluation.decisions)) {
    const message = evaluation?.state === 'unevaluated'
      ? 'Quota state not evaluated.' : 'Quota state unavailable.';
    state.innerHTML = `<h3>Quota state</h3><span class="quota-state-item unavailable">${escapeHtml(message)}</span>`;
  } else if (!evaluation.decisions.length) {
    state.innerHTML = evaluation.allowed
      ? '<h3>Quota state</h3><span class="quota-state-item ok">No active warnings or blocks reported.</span>'
      : '<h3>Quota state</h3><span class="quota-state-item unavailable">Quota state unavailable.</span>';
  } else {
    state.innerHTML = `<h3>Quota state</h3>${evaluation.decisions.map(decision => {
      const severity = decision.severity === 'block' ? 'Block' : 'Warning';
      return `<span class="quota-state-item ${escapeHtml(decision.severity || 'warning')}">${severity}: ${escapeHtml(decision.code || 'quota decision')}</span>`;
    }).join('')}`;
  }
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

async function request(url, options = {}, allowReauthRetry = true) {
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
    if (await stepUpReauthentication()) return request(url, options, false);
    throw new Error('Reauthentication required for this action.');
  }
  if (!response.ok) {
    if (response.status === 429) {
      const retryAfter = response.headers.get('Retry-After');
      throw new Error(`Too many lookup attempts; try again in ${retryAfter || 'a'} seconds.`);
    }
    throw new Error(apiErrorMessage(data, response.status));
  }
  return data;
}

function renderLookupResult(user) {
  const role = user.current_vault_role;
  const canAdd = role !== 'owner';
  document.querySelector('#lookup-result').innerHTML = `
    <div><strong>${escapeHtml(user.display_name)}</strong><small>@${escapeHtml(user.username)} · ${escapeHtml(roleLabel(role))}</small></div>
    ${canAdd ? `<form id="add-member-form" class="member-form"><label class="sr-only" for="member-role">Vault role</label><select id="member-role" name="role"><option value="operator">Operator</option><option value="viewer">Viewer</option></select><button type="submit">${role ? 'Update access' : 'Add member'}</button></form>` : '<p class="form-help">The primary owner cannot be assigned another role.</p>'}
  `;
  document.querySelector('#lookup-result').classList.remove('hidden');
  if (canAdd && role) document.querySelector('#member-role').value = role;
}

async function lookup(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    lookupUser = await request('/api/vault/user-lookup', {
      method: 'POST', body: JSON.stringify({username: form.get('username')})
    });
    renderLookupResult(lookupUser);
  } catch (error) {
    lookupUser = null;
    document.querySelector('#lookup-result').classList.add('hidden');
    show(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelector('#user-lookup-form').addEventListener('submit', lookup);

document.querySelector('#lookup-result').addEventListener('submit', async event => {
  if (event.target.id !== 'add-member-form' || !lookupUser) return;
  event.preventDefault();
  const form = new FormData(event.target);
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    await request('/api/vault/members', {
      method: 'POST',
      body: JSON.stringify({user_id: lookupUser.id, role: form.get('role')})
    });
    show('Vault access updated.');
    await loadMembers();
    lookupUser.current_vault_role = form.get('role');
    renderLookupResult(lookupUser);
  } catch (error) {
    show(error.message, true);
    button.disabled = false;
  }
});

function renderMember(member) {
  const transfer = member.role === 'owner' ? '' : `<button class="secondary" data-transfer-user="${member.id}" data-transfer-name="${escapeHtml(member.display_name)}" data-transfer-username="${escapeHtml(member.username)}">Transfer ownership</button>`;
  const remove = member.role === 'owner' ? '' : `<button class="secondary" data-remove-user="${member.id}">Remove</button>`;
  return `<div class="admin-row"><div><strong>${escapeHtml(member.display_name)}</strong><small>@${escapeHtml(member.username)} · ${escapeHtml(roleLabel(member.role))}</small></div><div class="row-actions">${transfer}${remove}</div></div>`;
}

async function loadMembers() {
  const data = await request('/api/vault/members');
  document.querySelector('#members-list').innerHTML = data.items.map(renderMember).join('') || '<p class="subtitle">No members assigned.</p>';
}

document.querySelector('#refresh-members').addEventListener('click', async () => {
  try { await loadMembers(); show('Members refreshed.'); }
  catch (error) { show(error.message, true); }
});

document.querySelector('#members-list').addEventListener('click', async event => {
  const remove = event.target.closest('[data-remove-user]');
  const transfer = event.target.closest('[data-transfer-user]');
  if (remove) {
    if (!window.confirm('Remove this user from the vault?')) return;
    try {
      await request(`/api/vault/members/${remove.dataset.removeUser}`, {method: 'DELETE'});
      show('Vault access removed.');
      await loadMembers();
    } catch (error) { show(error.message, true); }
  }
  if (transfer) {
    const dialog = document.querySelector('#transfer-dialog');
    document.querySelector('#transfer-user-id').value = transfer.dataset.transferUser;
    document.querySelector('#transfer-message').textContent = `Transfer primary ownership to ${transfer.dataset.transferName} (@${transfer.dataset.transferUsername})? This changes the vault authority and does not move stored files.`;
    document.querySelector('#transfer-confirm').checked = false;
    dialog.showModal();
  }
});

document.querySelector('#cancel-transfer').addEventListener('click', () => document.querySelector('#transfer-dialog').close());
document.querySelector('#transfer-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!document.querySelector('#transfer-confirm').checked) return;
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await request('/api/vault/transfer-owner', {
      method: 'POST',
      body: JSON.stringify({new_owner_user_id: Number(document.querySelector('#transfer-user-id').value)})
    });
    show('Ownership transferred.');
    window.setTimeout(() => { window.location.href = '/'; }, 500);
  } catch (error) {
    show(error.message, true);
    button.disabled = false;
  }
});

document.querySelector('#logout-button').addEventListener('click', async () => {
  try { await request('/api/logout', {method: 'POST'}); window.location.href = '/login'; }
  catch (error) { show(error.message, true); }
});

async function loadOwnerQuota() {
  const state = document.querySelector('#owner-quota-load-state');
  try {
    const data = await request('/api/vault/quotas');
    renderOwnerQuota(data);
    state.textContent = 'Quota status loaded.';
  } catch (error) {
    state.textContent = error.message;
    show(error.message, true);
  }
}

function renderOperationPolicy(data) {
  operationPolicy = data;
  const enabled = document.querySelector('#auto-local-cleanup');
  const days = document.querySelector('#local-retention-days');
  enabled.checked = Boolean(data.auto_local_cleanup);
  days.value = data.local_retention_days === null
    || data.local_retention_days === undefined ? '' : String(data.local_retention_days);
  days.disabled = !enabled.checked;
}

async function loadOperationPolicy() {
  const state = document.querySelector('#owner-operation-policy-load-state');
  try {
    renderOperationPolicy(await request('/api/vault/operation-policy'));
    state.textContent = 'Local Copy retention loaded.';
  } catch (error) {
    state.textContent = error.message;
    show(error.message, true);
  }
}

document.querySelector('#auto-local-cleanup')?.addEventListener('change', event => {
  document.querySelector('#local-retention-days').disabled = !event.currentTarget.checked;
});

document.querySelector('#operation-policy-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  const enabled = document.querySelector('#auto-local-cleanup').checked;
  const daysInput = document.querySelector('#local-retention-days');
  const retentionDays = daysInput.value === '' ? null : Number(daysInput.value);
  if (enabled && (!Number.isInteger(retentionDays) || retentionDays < 1)) {
    show('Enter at least one retention day before enabling automatic cleanup.', true);
    return;
  }
  button.disabled = true;
  try {
    const updated = await request('/api/vault/operation-policy', {
      method: 'PUT',
      body: JSON.stringify({
        ...operationPolicy,
        auto_local_cleanup: enabled,
        local_retention_days: retentionDays,
      }),
    });
    renderOperationPolicy(updated);
    show(enabled
      ? 'Automatic Local Copy retention enabled.'
      : 'Automatic Local Copy retention disabled.');
  } catch (error) {
    show(error.message, true);
  } finally {
    button.disabled = false;
  }
});

function profileLabel(name, profile) {
  if (!profile || !profile.transitions || !profile.transitions.length) {
    return `${name} · keep Standard`;
  }
  const steps = profile.transitions
    .map(step => `${step.storage_class} @ ${step.days}d`)
    .join(' → ');
  return `${name} · ${steps}`;
}

function fillProfileSelect(select, guidedProfiles, selected) {
  select.innerHTML = Object.entries(guidedProfiles).map(([name, profile]) => {
    const selectedAttr = name === selected ? ' selected' : '';
    return `<option value="${escapeHtml(name)}"${selectedAttr}>${escapeHtml(profileLabel(name, profile))}</option>`;
  }).join('');
}

function renderLifecycle(data) {
  const guided = data.guided_profiles || {};
  const defaultPolicy = (data.policies || []).find(item => item.id === data.default_policy_id);
  let selected = 'standard_only';
  if (defaultPolicy && defaultPolicy.profile) {
    const transitions = JSON.stringify(defaultPolicy.profile.transitions || []);
    for (const [name, profile] of Object.entries(guided)) {
      if (JSON.stringify(profile.transitions || []) === transitions) {
        selected = name;
        break;
      }
    }
  }
  fillProfileSelect(document.querySelector('#lifecycle-default-profile'), guided, selected);
  fillProfileSelect(document.querySelector('#lifecycle-folder-profile'), guided, 'archive_tiered');
  const overrides = data.folder_overrides || [];
  document.querySelector('#owner-lifecycle-overrides').innerHTML = overrides.length
    ? overrides.map(item => {
      const policy = (data.policies || []).find(row => row.id === item.policy_id);
      const label = policy ? policy.name : item.policy_id;
      return `<div class="admin-row"><div><strong>${escapeHtml(item.folder_path)}</strong><small>${escapeHtml(label)}</small></div><div class="row-actions"><button class="secondary" data-remove-override="${escapeHtml(item.folder_path)}" type="button">Remove</button></div></div>`;
    }).join('')
    : '<p class="subtitle">No folder overrides.</p>';
}

async function loadOwnerLifecycle() {
  const state = document.querySelector('#owner-lifecycle-load-state');
  try {
    const data = await request('/api/vault/lifecycle');
    renderLifecycle(data);
    state.textContent = 'Lifecycle policy loaded.';
  } catch (error) {
    state.textContent = error.message;
    show(error.message, true);
  }
}

document.querySelector('#lifecycle-default-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const data = await request('/api/vault/lifecycle/default', {
      method: 'PUT',
      body: JSON.stringify({
        guided_profile: document.querySelector('#lifecycle-default-profile').value,
      }),
    });
    renderLifecycle(data);
    const warnings = (data.warnings || []).join(' ');
    document.querySelector('#owner-lifecycle-warnings').textContent = warnings;
    show(warnings || 'Vault default lifecycle profile updated.');
  } catch (error) {
    show(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#lifecycle-override-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const data = await request('/api/vault/lifecycle/folder-overrides', {
      method: 'PUT',
      body: JSON.stringify({
        folder_path: document.querySelector('#lifecycle-folder-path').value,
        guided_profile: document.querySelector('#lifecycle-folder-profile').value,
      }),
    });
    renderLifecycle(data);
    document.querySelector('#lifecycle-folder-path').value = '';
    const warnings = (data.warnings || []).join(' ');
    document.querySelector('#owner-lifecycle-warnings').textContent = warnings;
    show(warnings || 'Folder lifecycle override updated.');
  } catch (error) {
    show(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#owner-lifecycle-overrides')?.addEventListener('click', async event => {
  const button = event.target.closest('[data-remove-override]');
  if (!button) return;
  try {
    const data = await request('/api/vault/lifecycle/folder-overrides', {
      method: 'DELETE',
      body: JSON.stringify({folder_path: button.dataset.removeOverride}),
    });
    renderLifecycle(data);
    show('Folder lifecycle override removed.');
  } catch (error) {
    show(error.message, true);
  }
});

async function loadCloudDeletionSetting() {
  const state = document.querySelector('#owner-cloud-deletion-load-state');
  const help = document.querySelector('#cloud-deletion-marker-help');
  const risk = document.querySelector('#cloud-deletion-risk');
  const checkbox = document.querySelector('#cloud-deletion-enabled');
  if (!state || !checkbox) return;
  try {
    const data = await request('/api/vault/cloud-deletion');
    checkbox.checked = Boolean(data.enabled);
    if (help) help.textContent = data.delete_marker_explanation;
    if (risk) risk.textContent = data.accepted_single_identity_risk || '';
    state.textContent = data.enabled
      ? 'Cloud deletion is enabled for this vault.'
      : 'Cloud deletion is disabled (default).';
  } catch (error) {
    state.textContent = error.message;
    show(error.message, true);
  }
}

document.querySelector('#cloud-deletion-setting-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const enabled = document.querySelector('#cloud-deletion-enabled').checked;
    const data = await request('/api/vault/cloud-deletion', {
      method: 'PUT',
      body: JSON.stringify({enabled}),
    });
    show(data.enabled ? 'Cloud deletion enabled.' : 'Cloud deletion disabled.');
    await loadCloudDeletionSetting();
  } catch (error) {
    show(error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function loadIdentity() {
  try {
    const data = await request('/api/me');
    authMethod = data.auth_method || null;
  } catch (_) {
    // Reauthentication falls back to a password prompt if identity loading fails.
  }
}

loadIdentity();
loadMembers().catch(error => show(error.message, true));
if (document.querySelector('main').dataset.role === 'owner') {
  loadOwnerQuota();
  loadOperationPolicy();
  loadOwnerLifecycle();
  loadCloudDeletionSetting();
}