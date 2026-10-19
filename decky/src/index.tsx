// SPDX-License-Identifier: GPL-3.0-or-later
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, Router, SliderField, TextField, ToggleField, staticClasses } from '@decky/ui';
import { callable, definePlugin, toaster } from '@decky/api';
import { useEffect, useRef, useState } from 'react';
import { Game, watchGames } from './events';
import { LaunchBackup, LaunchPanel, restoreLaunchBackups } from './LaunchPanel';

type Settings = Record<string, string | number>;
type Outcome = { notes: string[]; conflict?: boolean };
type Reply<T> = { ok: boolean; data: T; error?: string };
type State = {
  report: { profile: string; desired: Settings; update_pending: boolean; session: unknown; system: { model: string; kernel: string; readonly: string; model_specs: { ram_mt_s: number; battery_design_wh: number; panel_max_hz: number }; runtime: Record<string, string>; cpu: Record<string, Record<string, string>> }; managed: { change: { key: string; kind: string; path?: string }; last: string | null; actual: string | null; matches: boolean }[] };
  config: { automatic: boolean; games: Record<string, { name: string; settings: Settings; resume: boolean }>; launch_options?: Record<string, LaunchBackup> };
  profiles: Record<string, Settings>; catalog: Record<string, [string, string]>; runtime_keys: string[]; power_conflicts: string[];
};
const getState = callable<[], Reply<State>>('get_state');
const saveGame = callable<[string, string, Settings, boolean], Reply<Outcome>>('save_game');
const removeGame = callable<[string], Reply<Outcome>>('remove_game');
const activate = callable<[string, boolean], Reply<Outcome>>('activate');
const automatic = callable<[boolean], Reply<Outcome>>('set_automatic');
const preview = callable<[Settings, boolean], Reply<Outcome>>('preview');
const applyGlobal = callable<[string, Settings], Reply<Outcome>>('apply_global');
const release = callable<[string], Reply<Outcome>>('release');
const rollback = callable<[], Reply<Outcome>>('rollback');
const update = callable<[], Reply<Outcome>>('update');
const measurementTools = callable<[], Reply<Outcome>>('measurement_tools');
const cpuCheck = callable<[], Reply<Outcome>>('cpu_check');
const currentGame = (): Game => ({ id: String(Router.MainRunningApp?.appid || ''), name: Router.MainRunningApp?.display_name || '' });
function unpack<T>(reply: Reply<T>): T { if (!reply.ok) throw new Error(reply.error || 'Bosun error'); return reply.data; }
function notifyError(error: unknown) { toaster.toast({ title: 'Bosun', body: String(error), duration: 8000 }); }
function systemValue(key: string, value: string | null) {
  if (value === null) return 'недоступно';
  if (/^\d+$/.test(value) && key === 'cpu_max_mhz') return `${Number(value) / 1000} MHz`;
  if (/^\d+$/.test(value) && key === 'tdp_w') return `${Number(value) / 1000000} W`;
  return value.slice(0, 120);
}
async function syncGame(game: Game, resume = false) {
  const result = unpack(await activate(game.id, resume));
  if (result.conflict || result.notes.some(n => /CONFLICT|SKIP|DIVERGENCE/.test(n))) toaster.toast({ title: 'Bosun: проверьте профиль', body: result.notes.join('\n').slice(0, 700), duration: 8000 });
  window.dispatchEvent(new Event('bosun:game'));
  return result;
}
const labels: Record<string, string> = {
  mitigations: 'Программные CPU mitigations', nmi_watchdog: 'NMI watchdog', zram_page_cluster: 'Swap readahead zram', mglru: 'MGLRU', thp_defrag: 'Дефрагментация THP',
  thp: 'Политика THP', swappiness: 'Swappiness', governor: 'CPU governor', epp: 'CPU EPP', boost: 'CPU Boost', smt: 'SMT', cpu_max_mhz: 'Предел CPU, MHz', tdp_w: 'TDP APU, W', gpu_mode: 'GPU policy',
  nvme_scheduler: 'NVMe I/O scheduler', sd_scheduler: 'microSD I/O scheduler', moverate: 'AMDGPU migration MB/s', lbpw: 'LBPW: неприменим к Deck RDNA2', lavd: 'Scheduler LAVD',
  compaction_proactiveness: 'Фоновая компактация RAM', nvme_readahead_kb: 'NVMe read-ahead, KiB', sd_readahead_kb: 'microSD read-ahead, KiB', zram_algorithm: 'Компрессор zram (reboot)', zram_size_percent: 'Размер zram, % RAM (reboot)', workqueue_power_efficient: 'Workqueue power efficient (reboot)',
  amd_pstate: 'Режим AMD P-state (reboot)', gpu_sched_policy: 'GPU scheduler: 0 RR / 1 FIFO (reboot)', epp_boost: 'Per-core EPP boost (если есть, reboot)', home_noatime: 'Отключить atime для /home', usb_poll_ms: 'USB HID polling, ms (reboot)', memlock_kb: 'Лимит закреплённой RAM, KiB (reboot)',
  thp_shmem: 'THP для shared memory', dirty_background_mib: 'Фоновый writeback, MiB (пара)', dirty_mib: 'Прямой writeback, MiB (пара)', sched_autogroup: 'CPU autogroup', soft_watchdog: 'Soft watchdog', ksm: 'Сканирование KSM',
};

function Content() {
  const [data, setData] = useState<State>();
  const [loadError, setLoadError] = useState('');
  const [game, setGame] = useState(currentGame());
  const [scope, setScope] = useState('game');
  const [appid, setAppid] = useState(currentGame().id);
  const [name, setName] = useState(currentGame().name);
  const [draft, setDraft] = useState<Settings>({});
  const [dirty, setDirty] = useState(false);
  const [base, setBase] = useState('');
  const selection = useRef('');
  const refreshSequence = useRef(0);
  const [resume, setResume] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [cpuMessage, setCpuMessage] = useState('');
  const [key, setKey] = useState('compaction_proactiveness');
  const [value, setValue] = useState('0');
  const refresh = async () => {
    const sequence = ++refreshSequence.current;
    try { const next = unpack(await getState()); if (sequence === refreshSequence.current) { setData(next); setLoadError(''); } }
    catch (e) { if (sequence === refreshSequence.current) setLoadError(String(e)); throw e; }
  };
  useEffect(() => {
    refresh().catch(notifyError);
    const changed = () => { setGame(currentGame()); refresh().catch(notifyError); };
    window.addEventListener('bosun:game', changed);
    return () => { ++refreshSequence.current; window.removeEventListener('bosun:game', changed); };
  }, []);
  useEffect(() => {
    if (!data) return;
    const target = scope === 'global' ? 'global' : 'game:' + appid;
    if (selection.current === target && dirty) return;
    if (selection.current !== target) { setBase(''); setMessage(''); }
    selection.current = target;
    const saved = data.config.games[appid];
    setDraft(scope === 'global' ? { ...data.report.desired } : { ...(saved?.settings || {}) });
    if (scope === 'game') { setName(saved?.name || (appid === game.id ? game.name : '')); setResume(saved?.resume ?? true); }
    setDirty(false);
  }, [data, scope, appid, dirty, game.id, game.name]);
  const run = async (action: () => Promise<Reply<Outcome>>, commit = false) => {
    setBusy(true);
    const target = selection.current;
    let committed = false;
    try { const result = unpack(await action()); setMessage(result.notes.join('\n') || 'Готово'); committed = commit && !result.conflict; }
    catch (e) { setMessage(String(e)); }
    finally { try { await refresh(); if (committed && selection.current === target) setDirty(false); } catch (e) { setMessage(old => old + '\n' + String(e)); } setBusy(false); }
  };
  const set = (setting: string, next?: string | number) => { setDirty(true); setMessage(''); setDraft(old => {
    const result = { ...old }; if (next === undefined || next === '') delete result[setting]; else result[setting] = next;
    if (next === undefined && ['dirty_background_mib', 'dirty_mib'].includes(setting)) { delete result.dirty_background_mib; delete result.dirty_mib; }
    if (setting === 'epp' && next) result.governor = 'powersave';
    if (setting === 'governor' && next !== 'powersave') delete result.epp;
    return result;
  }); };
  const choose = (setting: string, options: string[]) => <PanelSectionRow><DropdownItem layout="below" label={labels[setting]} selectedOption={draft[setting] ?? ''} disabled={busy}
    rgOptions={[{ data: '', label: 'Наследовать / не менять' }, ...options.map(o => ({ data: o, label: o }))]}
    onChange={o => set(setting, String(o.data))} /></PanelSectionRow>;
  const slider = (setting: string, min: number, max: number, step: number, initial: number) => <>
    <PanelSectionRow><ToggleField label={`Управлять: ${labels[setting]}`} checked={draft[setting] !== undefined} disabled={busy} onChange={on => set(setting, on ? initial : undefined)} /></PanelSectionRow>
    {draft[setting] !== undefined && <PanelSectionRow><SliderField layout="below" label={labels[setting]} value={Number(draft[setting])} min={min} max={max} step={step} showValue editableValue disabled={busy} onChange={v => set(setting, v)} /></PanelSectionRow>}
  </>;
  if (!data) return <PanelSection><PanelSectionRow>{loadError || 'Загрузка Bosun…'}</PanelSectionRow><PanelSectionRow><ButtonItem layout="below" onClick={() => refresh().catch(notifyError)}>Повторить загрузку</ButtonItem></PanelSectionRow></PanelSection>;
  const system = data.report.system;
  const savedApps = [...new Set([...Object.keys(data.config.games), ...Object.values(data.config.launch_options || {}).map(saved => saved.appid)])];
  const keys = Object.keys(data.catalog).filter(k => k !== 'lbpw' && (scope === 'global' || data.runtime_keys.includes(k)));
  const add = () => {
    if (!keys.includes(key)) { setMessage('Выберите допустимую настройку'); return; }
    const parsed = /^-?\d+$/.test(value) ? Number(value) : value;
    set(key, parsed); setMessage('Параметр добавлен в черновик. Проверьте предпросмотр.');
  };
  return <>
    <PanelSection title={`Bosun 0.3.5 · ${system.model || 'неизвестная модель'}`}>
      <PanelSectionRow><div style={{ fontSize: 13 }}>RAM {system.model_specs.ram_mt_s} MT/s · батарея {system.model_specs.battery_design_wh} Wh · панель до {system.model_specs.panel_max_hz} Hz<br />Ядро {system.kernel}<br />CPU driver: {[...new Set(Object.values(system.cpu).map(p => p.scaling_driver))].join(', ')}<br />Профиль: {data.report.profile} · RO: {system.readonly}<br />{game.id ? `Игра: ${game.name} (${game.id})` : 'Игра не запущена'}</div></PanelSectionRow>
      {data.report.update_pending && <PanelSectionRow>SteamOS/стек обновился: проверьте глобальный профиль перед применением.</PanelSectionRow>}
      {data.power_conflicts.length > 0 && <PanelSectionRow>Конкурирующий менеджер питания: {data.power_conflicts.join(', ')}. Power-настройки будут пропущены.</PanelSectionRow>}
      <PanelSectionRow><ToggleField label="Автоматические профили игр" checked={data.config.automatic} disabled={busy} onChange={enabled => run(async () => {
        const result = unpack(await automatic(enabled));
        const applied = await syncGame(currentGame());
        return { ok: true, data: { notes: [...result.notes, ...applied.notes], conflict: applied.conflict } };
      })} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(async () => ({ ok: true, data: await syncGame(currentGame()) }))}>Синхронизировать текущую игру</ButtonItem></PanelSectionRow>
      <PanelSectionRow><DropdownItem layout="below" label="Редактировать" selectedOption={scope} disabled={busy} rgOptions={[{ data: 'game', label: 'Профиль игры' }, { data: 'global', label: 'Глобальные настройки' }]} onChange={o => setScope(String(o.data))} /></PanelSectionRow>
    </PanelSection>
    {scope === 'game' && <PanelSection title="Назначение игре">
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => setAppid(game.id)}>Выбрать запущенную игру</ButtonItem></PanelSectionRow>
      <PanelSectionRow><DropdownItem layout="below" label="Сохранённые игры и строки запуска" selectedOption={appid} disabled={busy} rgOptions={savedApps.map(id => ({ data: id, label: `${data.config.games[id]?.name || 'Steam'} (${id})` }))} onChange={o => setAppid(String(o.data))} /></PanelSectionRow>
      <PanelSectionRow><TextField label="AppID (можно указать до запуска игры)" value={appid} disabled={busy} onChange={e => setAppid(e.target.value)} /></PanelSectionRow>
      <PanelSectionRow><TextField label="Название" value={name} disabled={busy} onChange={e => { setName(e.target.value); setDirty(true); }} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="Один раз восстановить после сна" description="Только известные сбросы; без постоянной перезаписи настроек Steam" checked={resume} disabled={busy} onChange={v => { setResume(v); setDirty(true); }} /></PanelSectionRow>
    </PanelSection>}
    {scope === 'game' && <LaunchPanel appid={appid} backups={data.config.launch_options || {}} refresh={refresh} />}
    <PanelSection title="Настройки профиля">
      <PanelSectionRow>{dirty ? 'Есть неприменённые изменения. Предпросмотр их сохраняет; затем нажмите кнопку сохранения/применения ниже.' : 'Показаны сохранённые настройки выбранного профиля.'}</PanelSectionRow>
      {scope === 'game' && !data.config.automatic && <PanelSectionRow>Автоматические профили выключены: сохранение назначения не применит его к игре. Включите переключатель выше для применения при запуске.</PanelSectionRow>}
      <PanelSectionRow><DropdownItem layout="below" label="Загрузить основу в черновик" selectedOption={base} disabled={busy} strDefaultLabel="Выбрать профиль" rgOptions={Object.keys(data.profiles).map(p => ({ data: p, label: p }))}
        onChange={o => { const settings = data.profiles[String(o.data)]; setBase(String(o.data)); setDirty(true); setMessage(''); setDraft(Object.fromEntries(Object.entries(settings).filter(([k]) => scope === 'global' || data.runtime_keys.includes(k)))); }} /></PanelSectionRow>
      {choose('governor', ['powersave', 'performance', 'schedutil'])}
      {choose('epp', ['performance', 'balance_performance', 'balance_power', 'power'])}
      <PanelSectionRow><DropdownItem layout="below" label="CPU Boost" selectedOption={draft.boost ?? ''} disabled={busy} rgOptions={[{ data: '', label: 'Наследовать' }, { data: 0, label: 'Выключить' }, { data: 1, label: 'Включить' }]} onChange={o => set('boost', o.data === '' ? undefined : Number(o.data))} /></PanelSectionRow>
      {slider('cpu_max_mhz', 400, 3500, 100, 2800)}
      {slider('tdp_w', 4, 15, 1, 10)}
      {choose('gpu_mode', ['auto', 'low', 'high'])}
      <PanelSectionRow>Задайте FPS cap и refresh штатными средствами Steam; графика и разрешение этим профилем не снижаются.</PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => preview(draft, scope === 'game'))}>Предпросмотр без изменений</ButtonItem></PanelSectionRow>
      {scope === 'game' ? <>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || !appid} onClick={() => run(async () => {
          const result = unpack(await saveGame(appid, name, draft, resume));
          if (appid === currentGame().id && data.config.automatic) {
            const applied = await syncGame(currentGame());
            return { ok: true, data: { notes: [...result.notes, ...applied.notes], conflict: applied.conflict } };
          }
          return { ok: true, data: { notes: [...result.notes, data.config.automatic ? 'Назначение применится при запуске этой игры.' : 'SKIP: автоматические профили выключены; назначение сохранено без применения.'] } };
        }, true)}>{appid === game.id && data.config.automatic ? 'Сохранить и применить к текущей игре' : 'Сохранить назначение игре'}</ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || !appid} onClick={() => run(() => removeGame(appid), true)}>Удалить назначение</ButtonItem></PanelSectionRow>
      </> : <>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => applyGlobal('decky-global', draft), true)}>Применить глобально</ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => applyGlobal('recommended', data.profiles.recommended), true)}>Применить рекомендуемый профиль</ButtonItem></PanelSectionRow>
      </>}
      {dirty && <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => { setBase(''); setDirty(false); }}>Сбросить черновик к сохранённому профилю</ButtonItem></PanelSectionRow>}
      {message && <PanelSectionRow><div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }}>{message}</div></PanelSectionRow>}
    </PanelSection>
    <PanelSection title="Фактическое состояние устройства">
      <PanelSectionRow>Последнее чтение из системы; выбранные значения в черновике сами по себе не подтверждают применение.</PanelSectionRow>
      {Object.entries(system.cpu).map(([p, cpu]) => <PanelSectionRow key={p}><div style={{ fontSize: 13, overflowWrap: 'anywhere' }}>
        {p.split('/').pop()} · {cpu.scaling_driver} · {cpu.scaling_governor}<br />
        Диапазон CPUFreq: {systemValue('cpu_max_mhz', cpu.scaling_min_freq ?? null)} — {systemValue('cpu_max_mhz', cpu.scaling_max_freq ?? null)}<br />
        Текущая ({cpu.cpuinfo_cur_freq ? 'cpuinfo_cur_freq' : 'scaling_cur_freq'}): {systemValue('cpu_max_mhz', cpu.cpuinfo_cur_freq || cpu.scaling_cur_freq || null)}
      </div></PanelSectionRow>)}
      <PanelSectionRow>Совпадение CPUFreq с настройкой подтверждает запись policy, но не аппаратный потолок Boost. При превышении в оверлее выполните проверку ниже.</PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(async () => {
        const result = unpack(await cpuCheck());
        setCpuMessage(result.notes.join('\n'));
        return { ok: true, data: { notes: ['Результат проверки CPU — в блоке фактического состояния.'] } };
      })}>Проверить CPU: лимиты и частоты (2 с)</ButtonItem></PanelSectionRow>
      {cpuMessage && <PanelSectionRow><div style={{ fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{cpuMessage}</div></PanelSectionRow>}
      {data.report.managed.map((m, i) => <PanelSectionRow key={i}>
        <div style={{ fontSize: 13, overflowWrap: 'anywhere' }}>{m.change.path?.endsWith('/scaling_min_freq') ? 'Нижняя граница CPU (связана с пределом)' : labels[m.change.key] || m.change.key}: {['boot', 'zram_config'].includes(m.change.key) ? 'override сохранён; проверка эффекта после reboot' : <>ожидалось {systemValue(m.change.key, m.last)}; прочитано {systemValue(m.change.key, m.actual)} {m.matches ? '✓ совпадает' : '— расхождение'}</>}</div>
        {!m.matches && m.change.kind === 'scalar' && <ButtonItem layout="below" disabled={busy} onClick={() => run(() => release(m.change.key))}>Оставить фактическое значение</ButtonItem>}
      </PanelSectionRow>)}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => refresh().catch(notifyError)}>Обновить фактическое состояние</ButtonItem></PanelSectionRow>
    </PanelSection>
    <PanelSection title="Отдельные и экспериментальные параметры">
      <PanelSectionRow><DropdownItem layout="below" label="Параметр" selectedOption={key} disabled={busy} rgOptions={keys.map(k => ({ data: k, label: labels[k] || k }))} onChange={o => setKey(String(o.data))} /></PanelSectionRow>
      <PanelSectionRow><div style={{ fontSize: 12 }}>{data.catalog[key]?.[1]}</div></PanelSectionRow>
      <PanelSectionRow><TextField label={`Значение: ${data.catalog[key]?.[0] || ''}`} value={value} disabled={busy} onChange={e => setValue(e.target.value)} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={add}>Добавить / заменить в черновике</ButtonItem></PanelSectionRow>
      {Object.entries(draft).map(([k, v]) => <PanelSectionRow key={k}><ButtonItem layout="below" disabled={busy} onClick={() => set(k, undefined)}>{labels[k] || k}: {v} · убрать</ButtonItem></PanelSectionRow>)}
    </PanelSection>
    <PanelSection title="Состояние и обслуживание">
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(measurementTools)}>Подготовить MangoHud для A/B-замеров</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(async () => { await restoreLaunchBackups(data.config.launch_options || {}); return rollback(); }, true)}>Полный откат, включая параметры запуска Steam</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => update())}>Обновить Bosun и плагин</ButtonItem></PanelSectionRow>
    </PanelSection>
  </>;
}

export default definePlugin(() => {
  const stop = watchGames(SteamClient, currentGame, async (game, resume) => { await syncGame(game, resume); }, notifyError);
  // Decky otherwise unmounts the form when QAM hides, losing pending native dropdown callbacks and focus targets.
  return { name: 'Bosun', alwaysRender: true, titleView: <div className={staticClasses.Title}>Bosun</div>, content: <Content />, icon: <span>⚓</span>, onDismount: stop };
});
