import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const script = await readFile(new URL('../app/static/app.js', import.meta.url), 'utf8');
const englishCatalog = JSON.parse(
  await readFile(new URL('../app/locales/en.json', import.meta.url), 'utf8'),
);
const italianCatalog = JSON.parse(
  await readFile(new URL('../app/locales/it.json', import.meta.url), 'utf8'),
);

function createElement() {
  return {
    dataset: {canOperate: 'true', deleteEnabled: 'true'},
    innerHTML: '',
    textContent: '',
    disabled: false,
    value: '',
    checked: false,
    style: {},
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener() {},
    querySelector() { return createElement(); },
    querySelectorAll() { return []; },
    closest() { return null; },
    setAttribute() {},
  };
}

function loadApp(catalog = englishCatalog) {
  const elements = new Map();
  const catalogNode = {
    textContent: JSON.stringify(catalog),
  };
  const document = {
    cookie: '',
    getElementById(id) {
      if (id === 'i18n-catalog') return catalogNode;
      return null;
    },
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, createElement());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
  };
  const window = {
    location: {search: '', href: '', reload() {}, pathname: '/'},
    localStorage: {getItem() { return null; }, setItem() {}},
    history: {pushState() {}},
    addEventListener() {},
    clearTimeout() {},
    setTimeout() { return 0; },
  };
  const context = {
    console,
    document,
    window,
    URLSearchParams,
    JSON,
    FormData: class {},
    setTimeout: window.setTimeout,
    clearTimeout: window.clearTimeout,
    fetch: async () => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({items: [], groups: [], total: 0, vaults: []}),
    }),
  };
  const definitions = script.replace(
    /\r?\nloadIdentity\(\);\r?\nloadVaults\(\);\r?\nrenderBreadcrumbs\(\);\r?\nrefresh\(\);\s*$/,
    '\n',
  );
  vm.runInNewContext(
    `${definitions}\nthis.__exports = {operationStatusLabel, formatFileHistory, t};`,
    context,
  );
  return context.__exports;
}

test('operation status labels distinguish upload phases in English', () => {
  const {operationStatusLabel} = loadApp(englishCatalog);
  assert.equal(operationStatusLabel({action: 'upload', status: 'queued'}), 'Waiting');
  assert.equal(operationStatusLabel({action: 'upload', status: 'uploading'}), 'Uploading');
  assert.equal(operationStatusLabel({action: 'upload', status: 'verifying'}), 'Verifying');
  assert.equal(operationStatusLabel({action: 'upload', status: 'retrying'}), 'Retrying');
  assert.equal(operationStatusLabel({action: 'upload', status: 'failed'}), 'Failed');
  assert.equal(operationStatusLabel({action: 'upload', status: 'cancelled'}), 'Cancelled');
  assert.equal(operationStatusLabel({action: 'upload', status: 'completed'}), 'Verified');
});

test('operation status labels use the Italian catalog', () => {
  const {operationStatusLabel, t} = loadApp(italianCatalog);
  assert.equal(t('ui.sign_out'), 'Esci');
  assert.equal(operationStatusLabel({action: 'upload', status: 'queued'}), 'In attesa');
  assert.equal(operationStatusLabel({action: 'upload', status: 'completed'}), 'Verificato');
});

test('rename status labels and continuous history span old and new keys', () => {
  const {operationStatusLabel, formatFileHistory} = loadApp(englishCatalog);
  assert.equal(operationStatusLabel({action: 'rename', status: 'uploading'}), 'Uploading');
  assert.equal(operationStatusLabel({action: 'rename', status: 'verifying'}), 'Verifying');
  assert.equal(operationStatusLabel({action: 'rename', status: 'cleaning'}), 'Cleaning');
  assert.equal(operationStatusLabel({action: 'rename', status: 'completed'}), 'Renamed');
  const formatted = formatFileHistory({
    path: 'archive/new-name.txt',
    path_history: [
      {path: 'reports/old-name.txt'},
      {path: 'archive/new-name.txt'},
    ],
    versions: [
      {object_key: 'docs/archive/new-name.txt'},
      {object_key: 'docs/reports/old-name.txt'},
    ],
  });
  assert.equal(
    formatted.pathLabel,
    'reports/old-name.txt → archive/new-name.txt',
  );
  assert.match(formatted.summary, /docs\/reports\/old-name\.txt → docs\/archive\/new-name\.txt/);
  assert.match(formatted.summary, /2 versions/);
});
