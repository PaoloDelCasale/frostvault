import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const adminScript = await readFile(new URL('../app/static/admin.js', import.meta.url), 'utf8');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return {promise, resolve, reject};
}

function jsonResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(data),
  };
}

function createElement() {
  const handlers = new Map();
  return {
    innerHTML: '',
    textContent: '',
    disabled: false,
    formValues: {},
    open: false,
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener(type, handler) {
      handlers.set(type, handler);
    },
    dispatch(type, event) {
      return handlers.get(type)?.(event);
    },
    querySelector() {
      return createElement();
    },
    reset() {
      this.formValues = {};
    },
    setAttribute() {},
    showModal() { this.open = true; },
  };
}

async function createHarness({authMethod = 'oidc'} = {}) {
  const elements = new Map();
  const selectors = [
    '#admin-notice', '#owner-select', '#users-list', '#vaults-list',
    '#members-title', '#member-user', '#members-list', '#transfer-owner-user',
    '#transfer-owner-form', '#transfer-owner-form button[type="submit"]',
    '#members-dialog', '#user-form', '#vault-form', '#member-form',
    '#quota-form', '#quota-save', '#quota-load-state', '#admin-quota-usage',
    '#quota-form [name="storage_soft_limit_bytes"]', '#quota-form [name="storage_hard_limit_bytes"]',
    '#quota-form [name="concurrency_soft_limit"]', '#quota-form [name="concurrency_hard_limit"]',
    '#quota-form [name="restore_30d_soft_limit_bytes"]', '#quota-form [name="restore_30d_hard_limit_bytes"]',
    '#quota-form [name="reason"]',
  ];
  for (const selector of selectors) elements.set(selector, createElement());
  const noticeMessage = createElement();
  const noticeClose = createElement();
  elements.get('#admin-notice').querySelector = selector =>
    selector === '.notice-message' ? noticeMessage : noticeClose;
  elements.get('#transfer-owner-form').querySelector = selector =>
    selector === 'button[type="submit"]' ? elements.get('#transfer-owner-form button[type="submit"]') : createElement();
  elements.get('#quota-form').querySelector = selector => {
    const match = selector.match(/^\[name="([^"]+)"\]$/);
    return match ? elements.get(`#quota-form [name="${match[1]}"]`) : createElement();
  };
  elements.get('#quota-form').reset = () => {
    elements.get('#quota-form').formValues = {};
    for (const selector of selectors.filter(item => item.startsWith('#quota-form [name='))) {
      elements.get(selector).value = '';
    }
  };

  const memberRequests = new Map();
  const quotaRequests = [];
  const transferRequests = [];
  const document = {
    cookie: '',
    querySelector(selector) {
      const element = elements.get(selector);
      if (!element) throw new Error(`Unexpected selector: ${selector}`);
      return element;
    },
  };
  const window = {
    clearTimeout() {},
    setTimeout() { return 0; },
    prompt() { return 'correct password'; },
    confirm() { return false; },
    location: {pathname: '/admin', search: ''},
  };
  const fetch = async (url, options = {}) => {
    if (url === '/api/me') return jsonResponse({auth_method: authMethod});
    if (url === '/api/reauth') return jsonResponse({ok: true});
    if (url === '/api/admin/users') {
      return jsonResponse({items: [
        {id: 10, display_name: 'A user', username: 'a-user', active: true},
        {id: 20, display_name: 'B user', username: 'b-user', active: true},
      ]});
    }
    if (url === '/api/admin/vaults') {
      return jsonResponse({items: [
        {id: 1, name: 'Vault A'},
        {id: 2, name: 'Vault B'},
      ]});
    }
    const quotaMatch = url.match(/^\/api\/admin\/vaults\/(\d+)\/quotas$/);
    if (quotaMatch) {
      const request = deferred();
      quotaRequests.push({vaultId: Number(quotaMatch[1]), method: options.method || 'GET', options, ...request});
      return request.promise;
    }
    const match = url.match(/^\/api\/admin\/vaults\/(\d+)\/members$/);
    if (match) {
      const request = deferred();
      memberRequests.set(Number(match[1]), request);
      return request.promise;
    }
    if (url.match(/^\/api\/admin\/vaults\/\d+\/transfer-owner$/)) {
      const request = deferred();
      transferRequests.push({url, options, ...request});
      return request.promise;
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  vm.runInNewContext(adminScript, {
    console,
    document,
    fetch,
    window,
    URLSearchParams,
    JSON,
    FormData: class FormDataMock {
      constructor(form) {
        this.values = form.formValues;
      }
      entries() {
        return Object.entries(this.values);
      }
      get(name) {
        return this.values[name] ?? null;
      }
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return {elements, memberRequests, quotaRequests, transferRequests, noticeMessage};
}

function clickVault(harness, id, name) {
  const button = {
    dataset: {members: String(id), name},
    closest(selector) { return selector === '[data-members]' ? this : null; },
  };
  return harness.elements.get('#vaults-list').dispatch('click', {target: button});
}

function latestQuotaRequest(harness, vaultId, method = 'GET') {
  return [...harness.quotaRequests].reverse().find(request =>
    request.vaultId === vaultId && request.method === method
  );
}

function unlimitedQuotaResponse() {
  return jsonResponse({
    limits: {
      storage_soft_limit_bytes: null, storage_hard_limit_bytes: null,
      concurrency_soft_limit: null, concurrency_hard_limit: null,
      restore_30d_soft_limit_bytes: null, restore_30d_hard_limit_bytes: null,
    },
    usage: {storage_bytes: 0, concurrency: 0, restore_30d_bytes: 0},
    evaluation: {state: 'evaluated', allowed: true, decisions: []},
  });
}

const membersA = {items: [
  {id: 10, display_name: 'A member', username: 'a-member', active: true, role: 'operator'},
]};
const membersB = {items: [
  {id: 20, display_name: 'B member', username: 'b-member', active: true, role: 'operator'},
]};

const membersWithInactiveTarget = {items: [
  {id: 10, display_name: 'Current owner', username: 'owner', active: true, role: 'owner'},
  {id: 20, display_name: 'Inactive target', username: 'inactive-target', active: false, role: 'operator'},
  {id: 30, display_name: 'Active target', username: 'active-target', active: true, role: 'viewer'},
]};

test('inactive vault members never become ownership transfer targets', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 1, 'Vault A');

  harness.memberRequests.get(1).resolve(jsonResponse(membersWithInactiveTarget));
  await request;

  const transferTargets = harness.elements.get('#transfer-owner-user');
  assert.doesNotMatch(transferTargets.innerHTML, /Inactive target/);
  assert.match(transferTargets.innerHTML, /Active target/);
  assert.equal(transferTargets.disabled, false);
});

test('transfer cannot submit a stale member while a newer vault load is pending', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  await requestA;

  const requestB = clickVault(harness, 2, 'Vault B');
  const transferForm = harness.elements.get('#transfer-owner-form');
  transferForm.formValues = {
    new_owner_user_id: '10',
    reason: 'stale selection',
    confirmation: 'on',
  };
  await transferForm.dispatch('submit', {
    currentTarget: transferForm,
    preventDefault() {},
  });

  assert.equal(harness.transferRequests.length, 0);
  assert.equal(harness.elements.get('#transfer-owner-user').disabled, true);
  assert.equal(harness.noticeMessage.textContent, 'Wait for the current vault members to finish loading.');

  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  await requestB;
});

test('active member from the loaded vault can submit an ownership transfer', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 2, 'Vault B');
  const initialMemberRequest = harness.memberRequests.get(2);
  initialMemberRequest.resolve(jsonResponse(membersB));
  await request;

  const transferForm = harness.elements.get('#transfer-owner-form');
  transferForm.formValues = {
    new_owner_user_id: '20',
    reason: 'active owner transfer',
    confirmation: 'on',
  };
  const submission = transferForm.dispatch('submit', {
    currentTarget: transferForm,
    preventDefault() {},
  });

  await new Promise(resolve => setImmediate(resolve));
  harness.transferRequests[0].resolve(jsonResponse({}));
  await new Promise(resolve => setImmediate(resolve));
  const refreshMemberRequest = harness.memberRequests.get(2);
  assert.notEqual(refreshMemberRequest, initialMemberRequest);
  refreshMemberRequest.resolve(jsonResponse(membersB));
  await submission;

  assert.equal(harness.transferRequests.length, 1);
  assert.equal(harness.transferRequests[0].url, '/api/admin/vaults/2/transfer-owner');
  assert.deepEqual(JSON.parse(harness.transferRequests[0].options.body), {
    new_owner_user_id: 20,
    reason: 'active owner transfer',
  });
  assert.equal(transferForm.formValues.confirmation, undefined);
  assert.equal(harness.elements.get('#transfer-owner-form button[type="submit"]').disabled, false);
  assert.equal(harness.noticeMessage.textContent, 'Ownership transferred');
});

test('stale transfer success cannot mutate the newly selected vault', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  await requestA;

  const transferForm = harness.elements.get('#transfer-owner-form');
  transferForm.formValues = {
    new_owner_user_id: '10',
    reason: 'stale success',
    confirmation: 'on',
  };
  const submission = transferForm.dispatch('submit', {
    currentTarget: transferForm,
    preventDefault() {},
  });
  await new Promise(resolve => setImmediate(resolve));

  const requestB = clickVault(harness, 2, 'Vault B');
  harness.transferRequests[0].resolve(jsonResponse({}));
  await submission;

  assert.equal(harness.elements.get('#members-list').innerHTML, '');
  assert.equal(transferForm.formValues.reason, 'stale success');
  assert.equal(harness.noticeMessage.textContent, '');
  assert.equal(harness.elements.get('#transfer-owner-form button[type="submit"]').disabled, true);

  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  await requestB;
});

test('stale transfer error cannot mutate the newly selected vault', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  await requestA;

  const transferForm = harness.elements.get('#transfer-owner-form');
  transferForm.formValues = {
    new_owner_user_id: '10',
    reason: 'stale error',
    confirmation: 'on',
  };
  const submission = transferForm.dispatch('submit', {
    currentTarget: transferForm,
    preventDefault() {},
  });
  await new Promise(resolve => setImmediate(resolve));

  const requestB = clickVault(harness, 2, 'Vault B');
  harness.transferRequests[0].reject(new Error('Vault A transfer failed'));
  await submission;

  assert.equal(harness.elements.get('#members-list').innerHTML, '');
  assert.equal(transferForm.formValues.reason, 'stale error');
  assert.equal(harness.noticeMessage.textContent, '');
  assert.equal(harness.elements.get('#transfer-owner-form button[type="submit"]').disabled, true);

  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  await requestB;
});

test('out-of-order member success cannot replace the selected vault', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  const requestB = clickVault(harness, 2, 'Vault B');

  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  await requestB;
  const membersList = harness.elements.get('#members-list');
  const transferTargets = harness.elements.get('#transfer-owner-user');
  assert.match(membersList.innerHTML, /B member/);
  assert.match(transferTargets.innerHTML, /B member/);

  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  await requestA;
  assert.match(membersList.innerHTML, /B member/);
  assert.doesNotMatch(membersList.innerHTML, /A member/);
  assert.match(transferTargets.innerHTML, /B member/);
  assert.doesNotMatch(transferTargets.innerHTML, /A member/);
});

test('out-of-order member error cannot replace the selected vault or show an error', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  const requestB = clickVault(harness, 2, 'Vault B');

  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  await requestB;
  harness.memberRequests.get(1).reject(new Error('Vault A failed'));
  await requestA;

  assert.match(harness.elements.get('#members-list').innerHTML, /B member/);
  assert.equal(harness.noticeMessage.textContent, '');
});

test('blank quota limits render as unlimited and save as null', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(unlimitedQuotaResponse());
  await request;

  const form = harness.elements.get('#quota-form');
  form.formValues = {
    storage_soft_limit_bytes: '', storage_hard_limit_bytes: '',
    concurrency_soft_limit: '', concurrency_hard_limit: '',
    restore_30d_soft_limit_bytes: '', restore_30d_hard_limit_bytes: '',
    reason: 'remove quota limits',
  };
  const submission = form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  const save = latestQuotaRequest(harness, 1, 'PUT');
  assert.ok(save);
  save.resolve(unlimitedQuotaResponse());
  await submission;

  assert.deepEqual(JSON.parse(save.options.body), {
    storage_soft_limit_bytes: null, storage_hard_limit_bytes: null,
    concurrency_soft_limit: null, concurrency_hard_limit: null,
    restore_30d_soft_limit_bytes: null, restore_30d_hard_limit_bytes: null,
    reason: 'remove quota limits',
  });
  assert.equal(harness.elements.get('#quota-form [name="storage_soft_limit_bytes"]').value, '');
  assert.equal(harness.elements.get('#quota-form [name="storage_hard_limit_bytes"]').value, '');
  assert.equal(harness.elements.get('#quota-load-state').textContent, 'Quota status loaded.');
});

test('quota status formats backend decisions and identifies an unavailable evaluation', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(jsonResponse({
    limits: {}, usage: {},
    evaluation: {
      state: 'evaluated', allowed: true,
      decisions: [{code: 'quota.storage.soft_exceeded', severity: 'warning', projected: 11, limit: 10}],
    },
  }));
  await request;
  const usage = harness.elements.get('#admin-quota-usage').innerHTML;
  assert.match(usage, /Warning: quota\.storage\.soft_exceeded/);
  assert.doesNotMatch(usage, /No active warnings or blocks reported/);

  const next = clickVault(harness, 2, 'Vault B');
  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  latestQuotaRequest(harness, 2).resolve(jsonResponse({limits: {}, usage: {}}));
  await next;
  assert.match(harness.elements.get('#admin-quota-usage').innerHTML, /Quota state unavailable/);
});

test('invalid quota order and empty reason do not send or lose the form', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(unlimitedQuotaResponse());
  await request;
  const form = harness.elements.get('#quota-form');
  form.formValues = {
    storage_soft_limit_bytes: '9', storage_hard_limit_bytes: '4',
    concurrency_soft_limit: '', concurrency_hard_limit: '',
    restore_30d_soft_limit_bytes: '', restore_30d_hard_limit_bytes: '', reason: 'bad order',
  };
  await form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  assert.equal(harness.quotaRequests.filter(request => request.method === 'PUT').length, 0);
  assert.equal(form.formValues.storage_soft_limit_bytes, '9');
  assert.match(harness.noticeMessage.textContent, /cannot exceed/);

  form.formValues.storage_hard_limit_bytes = '10';
  form.formValues.reason = '';
  await form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  assert.equal(harness.quotaRequests.filter(request => request.method === 'PUT').length, 0);
  assert.equal(form.formValues.reason, '');
  assert.match(harness.noticeMessage.textContent, /reason/);
});

test('an invalid overlapping quota submission does not supersede a valid save', async () => {
  const harness = await createHarness();
  const request = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(unlimitedQuotaResponse());
  await request;

  const form = harness.elements.get('#quota-form');
  form.formValues = {
    storage_soft_limit_bytes: '10', storage_hard_limit_bytes: '20',
    concurrency_soft_limit: '', concurrency_hard_limit: '',
    restore_30d_soft_limit_bytes: '', restore_30d_hard_limit_bytes: '', reason: 'valid save',
  };
  const validSubmission = form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  const save = latestQuotaRequest(harness, 1, 'PUT');
  assert.ok(save);
  assert.equal(harness.elements.get('#quota-save').disabled, true);

  form.formValues.storage_hard_limit_bytes = '4';
  await form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  assert.equal(harness.noticeMessage.textContent, 'Soft storage limit cannot exceed the hard limit.');
  assert.equal(harness.elements.get('#quota-save').disabled, true);

  save.resolve(jsonResponse({
    limits: {storage_soft_limit_bytes: 10, storage_hard_limit_bytes: 20},
    usage: {storage_bytes: 0},
    evaluation: {state: 'evaluated', allowed: true, decisions: []},
  }));
  await validSubmission;
  assert.equal(harness.elements.get('#quota-save').disabled, false);
});

test('quota save retries after reauthentication and keeps API errors in the form', async () => {
  const harness = await createHarness({authMethod: null});
  const request = clickVault(harness, 1, 'Vault A');
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(unlimitedQuotaResponse());
  await request;
  const form = harness.elements.get('#quota-form');
  form.formValues = {
    storage_soft_limit_bytes: '10', storage_hard_limit_bytes: '20',
    concurrency_soft_limit: '1', concurrency_hard_limit: '2',
    restore_30d_soft_limit_bytes: '', restore_30d_hard_limit_bytes: '', reason: 'capacity policy',
  };
  const submission = form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  const first = latestQuotaRequest(harness, 1, 'PUT');
  first.resolve(jsonResponse({error: 'reauth_required'}, 403));
  await new Promise(resolve => setImmediate(resolve));
  const retry = [...harness.quotaRequests].filter(request => request.vaultId === 1 && request.method === 'PUT').at(-1);
  retry.resolve(jsonResponse({error: 'invalid quota'}, 422));
  await submission;

  assert.equal(harness.quotaRequests.filter(request => request.method === 'PUT').length, 2);
  assert.match(harness.noticeMessage.textContent, /invalid quota/);
  assert.equal(form.formValues.reason, 'capacity policy');
});

test('stale quota loads and saves cannot replace the selected vault', async () => {
  const harness = await createHarness();
  const requestA = clickVault(harness, 1, 'Vault A');
  const requestB = clickVault(harness, 2, 'Vault B');
  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  latestQuotaRequest(harness, 2).resolve(jsonResponse({limits: {storage_soft_limit_bytes: 22}, usage: {storage_bytes: 2}}));
  await requestB;
  harness.memberRequests.get(1).resolve(jsonResponse(membersA));
  latestQuotaRequest(harness, 1).resolve(jsonResponse({limits: {storage_soft_limit_bytes: 11}, usage: {storage_bytes: 1}}));
  await requestA;
  assert.equal(harness.elements.get('#quota-form [name="storage_soft_limit_bytes"]').value, '22');

  const form = harness.elements.get('#quota-form');
  form.formValues = {
    storage_soft_limit_bytes: '22', storage_hard_limit_bytes: '30',
    concurrency_soft_limit: '', concurrency_hard_limit: '',
    restore_30d_soft_limit_bytes: '', restore_30d_hard_limit_bytes: '', reason: 'stale save',
  };
  const saveSubmission = form.dispatch('submit', {currentTarget: form, preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  const staleSave = latestQuotaRequest(harness, 2, 'PUT');
  const selectB = clickVault(harness, 2, 'Vault B');
  staleSave.resolve(jsonResponse({limits: {storage_soft_limit_bytes: 999}, usage: {storage_bytes: 999}}));
  await saveSubmission;
  assert.equal(harness.elements.get('#quota-form [name="storage_soft_limit_bytes"]').value, '');
  assert.equal(harness.noticeMessage.textContent, '');
  harness.memberRequests.get(2).resolve(jsonResponse(membersB));
  latestQuotaRequest(harness, 2).resolve(unlimitedQuotaResponse());
  await selectB;
});
