import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const script = await readFile(new URL('../app/static/vault_access.js', import.meta.url), 'utf8');

function createElement() {
  const handlers = new Map();
  return {
    innerHTML: '',
    textContent: '',
    disabled: false,
    checked: false,
    value: '',
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener(type, handler) { handlers.set(type, handler); },
    dispatch(type, event) { return handlers.get(type)?.(event); },
    querySelector() { return createElement(); },
    setAttribute() {},
    closest() { return null; },
    showModal() {},
    close() {},
  };
}

function jsonResponse(data) {
  return {ok: true, status: 200, text: async () => JSON.stringify(data)};
}

async function createHarness(quotaResponse) {
  const selectors = [
    '#access-notice', '#user-lookup-form', '#lookup-result', '#refresh-members',
    '#members-list', '#transfer-dialog', '#cancel-transfer', '#transfer-form',
    '#transfer-user-id', '#transfer-message', '#transfer-confirm', '#logout-button',
    '#owner-quota-load-state', '#owner-quota-limits', '#owner-quota-usage', '#owner-quota-state',
    '#owner-lifecycle-load-state', '#lifecycle-default-form', '#lifecycle-default-profile',
    '#lifecycle-override-form', '#lifecycle-folder-path', '#lifecycle-folder-profile',
    '#owner-lifecycle-overrides', '#owner-lifecycle-warnings',
    '#owner-cloud-deletion-load-state', '#cloud-deletion-marker-help', '#cloud-deletion-risk',
    '#cloud-deletion-enabled', '#cloud-deletion-setting-form',
    '#owner-operation-policy-load-state', '#operation-policy-form',
    '#auto-local-cleanup', '#local-retention-days',
  ];
  const elements = new Map(selectors.map(selector => [selector, createElement()]));
  const noticeMessage = createElement();
  elements.get('#access-notice').querySelector = selector =>
    selector === '.notice-message' ? noticeMessage : createElement();
  const document = {
    cookie: '',
    querySelector(selector) {
      if (selector === 'main') return {dataset: {role: 'owner'}};
      const element = elements.get(selector);
      if (!element) throw new Error(`Unexpected selector: ${selector}`);
      return element;
    },
  };
  const window = {
    clearTimeout() {},
    setTimeout() { return 0; },
    prompt() { return ''; },
    location: {pathname: '/vault/access', search: ''},
  };
  const operationPolicyUpdates = [];
  const fetch = async (url, options = {}) => {
    if (url === '/api/me') return jsonResponse({auth_method: 'oidc'});
    if (url === '/api/vault/members') return jsonResponse({items: []});
    if (url === '/api/vault/quotas') return quotaResponse;
    if (url === '/api/vault/lifecycle') {
      return jsonResponse({
        default_policy_id: null,
        folder_overrides: [],
        policies: [],
        guided_profiles: {
          standard_only: {transitions: []},
          ia_after_30: {transitions: [{days: 30, storage_class: 'STANDARD_IA'}]},
        },
      });
    }
    if (url === '/api/vault/cloud-deletion') {
      return jsonResponse({
        enabled: false,
        purge_delay_seconds: 86400,
        delete_marker_explanation:
          'A Delete Marker is a reversible cloud marker that hides the current key.',
        generated_phrase: 'amber-birch-10',
        accepted_single_identity_risk: 'Single IAM identity risk documented.',
      });
    }
    if (url === '/api/vault/operation-policy') {
      if ((options.method || 'GET') === 'PUT') {
        const update = JSON.parse(options.body);
        operationPolicyUpdates.push(update);
        return jsonResponse(update);
      }
      return jsonResponse({
        auto_upload: true,
        auto_local_cleanup: true,
        local_retention_days: 45,
        stability_seconds: 300,
        include_globs: [],
        exclude_globs: [],
        bandwidth_limit_kibps: null,
        operating_windows: [],
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  vm.runInNewContext(script, {console, document, fetch, window, URLSearchParams, JSON, FormData: class {}});
  await new Promise(resolve => setImmediate(resolve));
  return {elements, operationPolicyUpdates};
}

test('owner quota state formats the authoritative backend decision', async () => {
  const harness = await createHarness(jsonResponse({
    limits: {}, usage: {},
    evaluation: {
      state: 'evaluated', allowed: false,
      decisions: [{code: 'quota.storage.hard_exceeded', severity: 'block', projected: 11, limit: 10}],
    },
  }));
  const state = harness.elements.get('#owner-quota-state').innerHTML;
  assert.match(state, /Block: quota\.storage\.hard_exceeded/);
  assert.doesNotMatch(state, /No active warnings or blocks reported/);
});

test('owner quota state does not invent an allow result when evaluation is unavailable', async () => {
  const harness = await createHarness(jsonResponse({limits: {}, usage: {}}));
  const state = harness.elements.get('#owner-quota-state').innerHTML;
  assert.match(state, /Quota state unavailable/);
  assert.doesNotMatch(state, /No active warnings or blocks reported/);
});

test('owner can configure automatic Local Copy retention without changing other policy fields', async () => {
  const harness = await createHarness(jsonResponse({
    limits: {}, usage: {},
    evaluation: {state: 'evaluated', allowed: true, decisions: []},
  }));
  assert.equal(harness.elements.get('#auto-local-cleanup').checked, true);
  assert.equal(harness.elements.get('#local-retention-days').value, '45');

  harness.elements.get('#local-retention-days').value = '60';
  const form = harness.elements.get('#operation-policy-form');
  await form.dispatch('submit', {
    currentTarget: form,
    preventDefault() {},
  });

  assert.deepEqual(harness.operationPolicyUpdates, [{
    auto_upload: true,
    auto_local_cleanup: true,
    local_retention_days: 60,
    stability_seconds: 300,
    include_globs: [],
    exclude_globs: [],
    bandwidth_limit_kibps: null,
    operating_windows: [],
  }]);
});
