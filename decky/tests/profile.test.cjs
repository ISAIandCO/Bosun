// SPDX-License-Identifier: GPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const Renderer = require('react-test-renderer');
const { act } = Renderer;
const copy = value => JSON.parse(JSON.stringify(value));

async function fixture({ automatic = true, saveError = '', activationNotes = [], built = false } = {}) {
  const calls = [], notices = [];
  const state = {
    report: { profile: 'stock', desired: {}, managed: [], update_pending: false, session: null,
      system: { model: 'LCD', kernel: 'fixture', readonly: 'enabled', runtime: {}, cpu: {}, model_specs: { ram_mt_s: 5500, battery_design_wh: 40, panel_max_hz: 60 } } },
    config: { automatic, games: { '456': { name: 'Other', settings: { governor: 'powersave', epp: 'balance_power' }, resume: false } }, launch_options: {} },
    profiles: { recommended: { nmi_watchdog: 0 }, 'cpu-game': { governor: 'powersave', epp: 'balance_performance' } },
    catalog: { governor: ['powersave|performance', 'CPU'], epp: ['balance_power|performance', 'EPP'], compaction_proactiveness: ['0..100', 'VM'] },
    runtime_keys: ['governor', 'epp', 'tdp_w', 'cpu_max_mhz', 'nmi_watchdog'], power_conflicts: [],
  };
  const rpc = async (method, ...args) => {
    calls.push([method, copy(args)]);
    if (method === 'get_state') return { ok: true, data: copy(state) };
    if (method === 'save_game') {
      if (saveError) return { ok: false, error: saveError };
      state.config.games[args[0]] = { name: args[1], settings: copy(args[2]), resume: args[3] };
      return { ok: true, data: { notes: ['Game profile saved'] } };
    }
    if (method === 'activate') {
      const saved = state.config.automatic && state.config.games[args[0]];
      if (saved) { state.report.desired = copy(saved.settings); state.report.profile = 'decky-' + args[0]; }
      return { ok: true, data: { notes: activationNotes } };
    }
    if (method === 'apply_global') { state.report.desired = copy(args[1]); state.report.profile = args[0]; }
    if (method === 'set_automatic') state.config.automatic = args[0];
    if (method === 'cpu_check') return { ok: true, data: { notes: ['CPU CHECK fixture: policy0 min/max=400000/2400000 kHz; peak=3000000 kHz', 'WARNING: hardware enforcement is NOT confirmed'] } };
    return { ok: true, data: { notes: [method === 'preview' ? 'PLAN' : 'Applied'] } };
  };
  const window = new EventTarget();
  window.App = { m_CurrentUser: { strSteamID: '76561198000000001' } };
  const api = { _version: 2, callable: name => (...args) => rpc(name, ...args), toaster: { toast: value => notices.push(value) } };
  window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit = { connect: () => api };
  const SteamClient = { Apps: { RegisterForAppDetails: (_id, callback) => { callback({ strLaunchOptions: '%command%' }); return { unregister() {} }; } } };
  const ui = Object.fromEntries(['ButtonItem', 'DropdownItem', 'PanelSection', 'PanelSectionRow', 'SliderField', 'TextField', 'ToggleField'].map(name => [name, name]));
  ui.Router = { MainRunningApp: { appid: 123, display_name: 'Current' } };
  ui.staticClasses = {};
  const cache = new Map();
  const load = filename => {
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} }; cache.set(filename, module);
    const requireMock = name => {
      if (name === '@decky/ui') return ui;
      if (name === '@decky/api') return { definePlugin: fn => fn, ...api };
      if (name.startsWith('.')) {
        const base = path.resolve(path.dirname(filename), name);
        return load(['.ts', '.tsx'].map(ext => base + ext).find(fs.existsSync));
      }
      return require(name);
    };
    const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 } }).outputText;
    vm.runInNewContext(code, { module, exports: module.exports, require: requireMock, window, SteamClient, Event, setTimeout, clearTimeout, console,
      SP_REACT: React, SP_JSX: require('react/jsx-runtime'), DFL: ui }, { filename });
    return module.exports;
  };
  const plugin = load(path.resolve(__dirname, built ? '../dist/index.js' : '../src/index.tsx')).default();
  // Decky PluginView unmounts content when QAM hides unless alwaysRender is set.
  // https://github.com/SteamDeckHomebrew/decky-loader/blob/67196ad/frontend/src/components/PluginView.tsx
  const host = visible => React.createElement('PluginHost', null, React.createElement('PluginTitle'),
    React.createElement('PluginBody', null, (visible || plugin.alwaysRender) && plugin.content));
  let view;
  await act(async () => { view = Renderer.create(host(true)); });
  const visible = async value => { await act(async () => { view.update(host(value)); }); };
  const field = (type, label) => view.root.findAllByType(type).find(n => n.props.label === label);
  const action = async text => {
    const node = view.root.findAllByType('ButtonItem').find(n => typeof n.props.children === 'string' && (typeof text === 'string' ? n.props.children === text : text.test(n.props.children)));
    assert.ok(node, 'Button not found: ' + text);
    await act(async () => { await node.props.onClick(); });
  };
  return { state, calls, notices, field, action, visible, buttons: () => view.root.findAllByType('ButtonItem'),
    section: title => view.root.findAllByType('PanelSection').find(n => n.props.title === title), text: () => JSON.stringify(view.toJSON()),
    dropdownModal: async (label, data) => {
      const onChange = field('DropdownItem', label).props.onChange;
      await visible(false);
      await act(async () => { onChange({ data }); });
      await visible(true);
    },
    dropdown: async (label, data) => { await act(async () => { field('DropdownItem', label).props.onChange({ data }); }); },
    toggle: async (label, value) => { await act(async () => { field('ToggleField', label).props.onChange(value); }); },
    slider: async (label, value) => { await act(async () => { field('SliderField', label).props.onChange(value); }); },
    input: async (label, value) => { await act(async () => { field('TextField', label).props.onChange({ target: { value } }); }); },
    refreshEvent: async () => { await act(async () => { window.dispatchEvent(new Event('bosun:game')); }); },
    close: async () => { await act(async () => { plugin.onDismount(); view.unmount(); }); },
  };
}

test('selected governor survives preview and reaches save and activation for the current game', async () => {
  const f = await fixture();
  try {
    await f.dropdown('CPU governor', 'performance');
    assert.equal(f.field('DropdownItem', 'CPU governor').props.selectedOption, 'performance');
    await f.action('Предпросмотр без изменений');
    assert.equal(f.field('DropdownItem', 'CPU governor').props.selectedOption, 'performance');
    await f.action(/^Сохранить.*(игр|назнач)/);
    assert.equal(f.calls.find(c => c[0] === 'save_game')[1][2].governor, 'performance');
    assert.equal(f.state.report.desired.governor, 'performance');
  } finally { await f.close(); }
});

test('unsaved TDP survives manual state refresh and game notifications', async () => {
  const f = await fixture();
  try {
    await f.toggle('Управлять: TDP APU, W', true);
    await f.slider('TDP APU, W', 9);
    await f.action('Обновить фактическое состояние');
    await f.refreshEvent();
    assert.equal(f.field('SliderField', 'TDP APU, W')?.props.value, 9);
  } finally { await f.close(); }
});

test('failed save retains edited EPP and exposes the error', async () => {
  const f = await fixture({ saveError: 'fixture save error' });
  try {
    await f.dropdown('CPU EPP', 'balance_power');
    await f.action(/^Сохранить.*(игр|назнач)/);
    assert.equal(f.field('DropdownItem', 'CPU EPP').props.selectedOption, 'balance_power');
    assert.ok(f.text().includes('fixture save error'), 'save error must be visible');
  } finally { await f.close(); }
});

test('changing the selected AppID loads its own saved profile', async () => {
  const f = await fixture();
  try {
    await f.dropdown('CPU governor', 'performance');
    await f.input('AppID (можно указать до запуска игры)', '456');
    assert.equal(f.field('DropdownItem', 'CPU EPP').props.selectedOption, 'balance_power');
  } finally { await f.close(); }
});

test('loading a base profile keeps its name and settings visible', async () => {
  const f = await fixture();
  try {
    await f.dropdown('Загрузить основу в черновик', 'cpu-game');
    await f.action('Предпросмотр без изменений');
    assert.equal(f.field('DropdownItem', 'Загрузить основу в черновик').props.selectedOption, 'cpu-game');
    assert.equal(f.field('DropdownItem', 'CPU EPP').props.selectedOption, 'balance_performance');
  } finally { await f.close(); }
});

test('application skips are visible in the profile result after saving', async () => {
  const f = await fixture({ activationNotes: ['SKIP tdp_w: competing power manager'] });
  try {
    await f.dropdown('CPU governor', 'performance');
    await f.action(/^Сохранить.*(игр|назнач)/);
    assert.ok(f.text().includes('SKIP tdp_w: competing power manager'), 'application skip must be visible');
  } finally { await f.close(); }
});

test('global edits survive preview and are sent to the global apply operation', async () => {
  const f = await fixture();
  try {
    await f.dropdown('Редактировать', 'global');
    await f.dropdown('CPU governor', 'performance');
    await f.action('Предпросмотр без изменений');
    assert.equal(f.field('DropdownItem', 'CPU governor').props.selectedOption, 'performance');
    await f.action('Применить глобально');
    assert.equal(f.calls.find(c => c[0] === 'apply_global')[1][1].governor, 'performance');
    assert.equal(f.field('DropdownItem', 'CPU governor').props.selectedOption, 'performance');
    assert.ok(!f.text().includes('Есть неприменённые изменения'));
  } finally { await f.close(); }
});

test('disabled automation explains save-only behavior and enabling applies the saved profile', async () => {
  const f = await fixture({ automatic: false });
  const activations = f.calls.filter(c => c[0] === 'activate').length;
  try {
    await f.dropdown('CPU governor', 'performance');
    await f.action('Сохранить назначение игре');
    assert.equal(f.calls.filter(c => c[0] === 'activate').length, activations);
    assert.ok(f.text().includes('назначение сохранено без применения'));
    await f.toggle('Автоматические профили игр', true);
    assert.equal(f.field('ToggleField', 'Автоматические профили игр').props.checked, true);
    assert.equal(f.state.report.desired.governor, 'performance');
  } finally { await f.close(); }
});

test('discarding a draft reloads saved settings without applying it', async () => {
  const f = await fixture();
  const actions = () => f.calls.filter(c => ['activate', 'save_game', 'apply_global'].includes(c[0])).length;
  const initial = actions();
  try {
    await f.toggle('Управлять: TDP APU, W', true);
    await f.slider('TDP APU, W', 9);
    await f.action('Сбросить черновик к сохранённому профилю');
    assert.equal(f.field('ToggleField', 'Управлять: TDP APU, W').props.checked, false);
    assert.equal(actions(), initial);
  } finally { await f.close(); }
});

for (const built of [false, true]) {
  const target = built ? 'release bundle' : 'source';
  test(`${target}: native dropdown QAM hide/show retains the field and sends its choice to the running game`, async () => {
    const f = await fixture({ built });
    try {
      const control = f.field('DropdownItem', 'CPU governor');
      const reads = f.calls.filter(c => c[0] === 'get_state').length;
      await f.dropdownModal('CPU governor', 'performance');
      assert.equal(f.field('DropdownItem', 'CPU governor').props.selectedOption, 'performance');
      assert.equal(f.field('DropdownItem', 'CPU governor'), control, 'focus target must not be remounted');
      await f.toggle('Управлять: TDP APU, W', true);
      await f.slider('TDP APU, W', 9);
      await f.visible(false);
      await f.visible(true);
      assert.equal(f.field('SliderField', 'TDP APU, W').props.value, 9);
      assert.equal(f.calls.filter(c => c[0] === 'get_state').length, reads, 'hiding QAM must not reload the form');
      await f.action('Прочитать параметры из Steam');
      await f.dropdownModal('I/O приоритет процесса (best-effort)', 0);
      assert.equal(f.field('DropdownItem', 'I/O приоритет процесса (best-effort)').props.selectedOption, 0);
      await f.input('Текущая строка / редактирование', 'MANGOHUD=1 %command%');
      await f.visible(false);
      await f.visible(true);
      assert.equal(f.field('TextField', 'Текущая строка / редактирование').props.value, 'MANGOHUD=1 %command%');
      await f.action(/^Сохранить.*(игр|назнач)/);
      assert.equal(f.state.report.desired.governor, 'performance');
      assert.equal(f.state.report.desired.tdp_w, 9);
    } finally { await f.close(); }
  });

  test(`${target}: actions use full-width layout in panel rows, including launch options and global controls`, async () => {
    const f = await fixture({ built });
    const check = () => {
      for (const button of f.buttons()) {
        assert.equal(button.props.layout, 'below', 'action must not reserve an empty label column: ' + button.props.children);
        assert.equal(button.parent.type, 'PanelSectionRow', 'action needs a QAM row');
      }
    };
    try {
      await f.action('Прочитать параметры из Steam');
      await f.dropdown('CPU governor', 'performance');
      check();
      await f.dropdown('Редактировать', 'global');
      check();
    } finally { await f.close(); }
  });

  test(`${target}: system readback displays actual CPU and TDP separately from the draft`, async () => {
    const f = await fixture({ built });
    try {
      f.state.report.managed = [
        { change: { key: 'cpu_max_mhz', kind: 'scalar' }, last: '2800000', actual: '3000000', matches: false },
        { change: { key: 'tdp_w', kind: 'scalar' }, last: '9000000', actual: '9000000', matches: true },
      ];
      await f.toggle('Управлять: Предел CPU, MHz', true);
      await f.slider('Предел CPU, MHz', 2400);
      await f.action('Обновить фактическое состояние');
      const actual = f.section('Фактическое состояние устройства');
      const text = JSON.stringify(actual.findAllByType('div').map(n => n.children));
      assert.ok(text.includes('2800 MHz') && text.includes('3000 MHz'), 'readback must use reported values, not the 2400 MHz draft');
      assert.ok(text.includes('расхождение'));
      assert.ok(text.includes('9 W') && text.includes('совпадает'));
      assert.equal(f.field('SliderField', 'Предел CPU, MHz').props.value, 2400);
      for (const button of actual.findAllByType('ButtonItem')) {
        assert.equal(button.props.layout, 'below');
        assert.equal(button.parent.type, 'PanelSectionRow');
      }
    } finally { await f.close(); }
  });

  test(`${target}: CPU check shows observations beside the live policies without changing the draft`, async () => {
    const f = await fixture({ built });
    try {
      await f.toggle('Управлять: Предел CPU, MHz', true);
      await f.slider('Предел CPU, MHz', 2400);
      await f.action('Проверить CPU: лимиты и частоты (2 с)');
      const actual = f.section('Фактическое состояние устройства');
      const text = JSON.stringify(actual.findAllByType('div').map(n => n.children));
      assert.ok(text.includes('CPU CHECK fixture') && text.includes('NOT confirmed'));
      assert.equal(f.field('SliderField', 'Предел CPU, MHz').props.value, 2400);
      assert.equal(f.calls.filter(c => c[0] === 'cpu_check').length, 1);
      assert.equal(f.calls.filter(c => c[0] === 'save_game').length, 0);
    } finally { await f.close(); }
  });
}
