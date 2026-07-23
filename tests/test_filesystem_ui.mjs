import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../app/static/app.js', import.meta.url), 'utf8');
const englishCatalog = JSON.parse(
  await readFile(new URL('../app/locales/en.json', import.meta.url), 'utf8'),
);

function loadFileStateLabel() {
  const helpersStart = source.indexOf('function t(key, params = {})');
  assert.ok(helpersStart >= 0, 't() must exist');
  const fileStateStart = source.indexOf('function fileStateLabel');
  assert.ok(fileStateStart >= 0, 'fileStateLabel must exist');
  const fileStateEnd = source.indexOf('\nfunction ', fileStateStart + 1);
  // Include t(), stateLabel(), and fileStateLabel() from the i18n-aware helpers.
  const stateLabelStart = source.indexOf('function stateLabel');
  assert.ok(stateLabelStart >= 0, 'stateLabel must exist');
  const fnSource = [
    source.slice(helpersStart, source.indexOf('\nfunction ', helpersStart + 1)),
    source.slice(stateLabelStart, source.indexOf('\nfunction ', stateLabelStart + 1)),
    source.slice(fileStateStart, fileStateEnd > fileStateStart ? fileStateEnd : undefined),
  ].join('\n');
  const sandbox = {
    i18nCatalog: englishCatalog,
  };
  vm.runInNewContext(
    `${fnSource}\nthis.fileStateLabel = fileStateLabel;`,
    sandbox,
  );
  return sandbox.fileStateLabel;
}

test('fileStateLabel reports symbolic links as rejected', () => {
  const fileStateLabel = loadFileStateLabel();
  const state = fileStateLabel({
    local_file_type: 'symlink',
    local_exists: 1,
    state: 'local_only',
  });
  assert.equal(state.className, 'unsupported');
  assert.match(state.label, /symbolic link/i);
});

test('fileStateLabel keeps regular file states', () => {
  const fileStateLabel = loadFileStateLabel();
  const state = fileStateLabel({
    local_file_type: 'regular',
    local_exists: 1,
    state: 'both',
  });
  assert.equal(state.className, 'both');
  assert.equal(state.label, 'Server + cloud');
});
