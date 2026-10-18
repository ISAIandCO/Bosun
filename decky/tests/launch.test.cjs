// SPDX-License-Identifier: GPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ts = require('typescript');
const sandbox = { exports: {}, setTimeout, clearTimeout, Promise, Error };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/launch.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText, sandbox);
const { readLaunch, writeLaunch } = sandbox.exports;
function fixture(shortcut = false) {
  let text = '--original'; let closed = 0; const writes = [];
  const apps = {
    RegisterForAppDetails: (id, callback) => {
      callback({ strLaunchOptions: shortcut ? '' : text, strShortcutLaunchOptions: shortcut ? text : '', strShortcutExe: shortcut ? '/game' : '' });
      return { unregister: () => closed++ };
    },
    SetAppLaunchOptions: (id, value) => { writes.push(['steam', id, value]); text = value; },
    SetShortcutLaunchOptions: (id, value) => { writes.push(['shortcut', id, value]); text = value; },
  };
  return { apps, writes, closed: () => closed };
}
test('read Steam launch options and unsubscribe even for a synchronous callback', async () => {
  const f = fixture(); const r = await readLaunch(f.apps, 123);
  assert.equal(r.text, '--original'); assert.equal(r.shortcut, false); assert.equal(f.closed(), 1);
});
test('write Steam launch options through the native API and verify', async () => {
  const f = fixture(); assert.equal(await writeLaunch(f.apps, 123, '--original', '--new'), '--new');
  assert.deepEqual(f.writes, [['steam', 123, '--new']]); assert.equal(f.closed(), 2);
});
test('non-Steam shortcuts use their own native setter', async () => {
  const f = fixture(true); await writeLaunch(f.apps, 4000000000, '--original', '--new');
  assert.deepEqual(f.writes, [['shortcut', 4000000000, '--new']]);
});
test('external change prevents overwrite', async () => {
  const f = fixture(); await assert.rejects(writeLaunch(f.apps, 123, '--stale', '--new'), /changed/);
  assert.equal(f.writes.length, 0);
});
test('account switch prevents overwrite', async () => {
  const f = fixture(); await assert.rejects(writeLaunch(f.apps, 123, '--original', '--new', () => false), /account changed/);
  assert.equal(f.writes.length, 0);
});
test('partial AppDetails is not mistaken for an empty launch string', async () => {
  let callback; let closed = 0;
  const promise = readLaunch({ RegisterForAppDetails: (id, cb) => { callback = cb; cb({}); return { unregister: () => closed++ }; } }, 123);
  callback({ strLaunchOptions: '--complete' });
  assert.equal((await promise).text, '--complete'); assert.equal(closed, 1);
});
