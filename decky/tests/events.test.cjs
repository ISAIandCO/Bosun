// SPDX-License-Identifier: GPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/events.ts'), 'utf8');

function fixture() {
  let current = { id: '123', name: 'Game' };
  let lifetime, resume, unregistered = 0;
  const timers = new Map(); let number = 0;
  const sandbox = { exports: {}, setTimeout: fn => { timers.set(++number, fn); return number; }, clearTimeout: id => timers.delete(id) };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, sandbox);
  const calls = [], errors = [];
  const stop = sandbox.exports.watchGames({
    GameSessions: { RegisterForAppLifetimeNotifications: fn => { lifetime = fn; return { unregister: () => unregistered++ }; } },
    System: { RegisterForOnResumeFromSuspend: fn => { resume = fn; return { unregister: () => unregistered++ }; } },
  }, () => current, async (g, r) => { calls.push([g.id, r]); }, e => errors.push(e));
  const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  return { calls, errors, stop, settle, lifetime: (...args) => lifetime(...args), resume: () => resume(),
    set: game => { current = game; }, unregistered: () => unregistered,
    flush: () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); } };
}

test('initial current game sync without periodic polling', async () => {
  const f = fixture(); await f.settle(); assert.deepEqual(f.calls, [['123', false]]); assert.deepEqual(f.errors, []); f.stop();
});
test('start and exit notifications select and restore profile', async () => {
  const f = fixture(); await f.settle();
  f.set({ id: '456', name: 'Other' }); f.lifetime({ unAppID: 456, bRunning: true }); f.flush(); await f.settle();
  f.lifetime({ unAppID: 456, bRunning: false }); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false], ['456', false], ['', false]]); f.stop();
});
test('non-Steam exit with AppID zero restores profile', async () => {
  const f = fixture(); await f.settle(); f.lifetime({ unAppID: 0, bRunning: false }); f.flush(); await f.settle();
  assert.equal(f.calls.at(-1)[0], ''); f.stop();
});
test('resume performs exactly one replay event', async () => {
  const f = fixture(); await f.settle(); f.resume(); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false], ['123', true]]); f.stop();
});
test('unload cancels deferred events and unregisters listeners', async () => {
  const f = fixture(); await f.settle(); f.resume(); f.stop(); f.flush(); await f.settle();
  assert.equal(f.calls.length, 1); assert.equal(f.unregistered(), 2);
});
test('startup and plugin reload each have one bounded follow-up, never a polling loop', async () => {
  const f = fixture(); await f.settle();
  f.flush(); await f.settle(); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false], ['123', false]]);
  f.set({ id: '456', name: 'Other' }); f.lifetime({ unAppID: 456, bRunning: true });
  f.flush(); await f.settle(); f.flush(); await f.settle(); f.flush(); await f.settle();
  assert.deepEqual(f.calls.slice(2), [['456', false], ['456', false]]); f.stop();
});
test('start uses lifetime AppID even when Router still reports the previous game', async () => {
  const f = fixture(); await f.settle();
  f.lifetime({ unAppID: 456, bRunning: true }); f.flush(); await f.settle();
  assert.equal(f.calls.at(-1)[0], '456'); f.stop();
});
test('switch cancels old follow-up and ignores a delayed exit from the old game', async () => {
  const f = fixture(); await f.settle();
  f.lifetime({ unAppID: 456, bRunning: true }); f.flush(); await f.settle();
  f.set({ id: '789', name: 'New' }); f.lifetime({ unAppID: 789, bRunning: true });
  f.lifetime({ unAppID: 456, bRunning: false });
  f.flush(); await f.settle(); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false], ['456', false], ['789', false], ['789', false]]); f.stop();
});
test('a queued start superseded by exit must never reapply the old game', async () => {
  const f = fixture(); await f.settle();
  f.set({ id: '456', name: 'Other' }); f.lifetime({ unAppID: 456, bRunning: true }); f.flush();
  f.lifetime({ unAppID: 456, bRunning: false }); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false], ['', false]]); f.stop();
});
test('unload cancels startup follow-up', async () => {
  const f = fixture(); await f.settle(); f.stop(); f.flush(); await f.settle();
  assert.deepEqual(f.calls, [['123', false]]);
});
