// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useState } from 'react';
import { ButtonItem, DropdownItem, PanelSectionRow, TextField } from '@decky/ui';
import { callable } from '@decky/api';
import { readLaunch, writeLaunch } from './launch';

type Reply<T> = { ok: boolean; data: T; error?: string };
type Outcome = { notes: string[]; target?: string };
export type LaunchBackup = { account: string; appid: string; original: string; last: string; pending: boolean };
type Options = Record<string, string | number | boolean>;
const preview = callable<[string, string, string, Options], Reply<Outcome>>('preview_launch');
const prepare = callable<[string, string, string, string, string, Options | null], Reply<Outcome>>('prepare_launch');
const finish = callable<[string, string, string], Reply<Outcome>>('finish_launch');
const account = () => window.App?.m_CurrentUser?.strSteamID || '';
const unpack = <T,>(r: Reply<T>): T => { if (!r.ok) throw new Error(r.error || 'Bosun error'); return r.data; };

export async function restoreLaunchBackups(backups: Record<string, LaunchBackup>) {
  for (const saved of Object.values(backups)) {
    if (saved.account !== account()) throw new Error('Для полного отката войдите в Steam-аккаунт ' + saved.account + ' и восстановите его параметры запуска');
    const current = await readLaunch(SteamClient.Apps, Number(saved.appid));
    const result = unpack(await prepare(saved.account, saved.appid, current.text, '', 'restore', null));
    const actual = await writeLaunch(SteamClient.Apps, Number(saved.appid), current.text, result.target!, () => account() === saved.account);
    unpack(await finish(saved.account, saved.appid, actual));
  }
}

export function LaunchPanel({ appid, backups, refresh }: { appid: string; backups: Record<string, LaunchBackup>; refresh: () => Promise<void> }) {
  const [loaded, setLoaded] = useState(false);
  const [text, setText] = useState('');
  const [baseline, setBaseline] = useState('');
  const [baselineKey, setBaselineKey] = useState('');
  const [options, setOptions] = useState<Options>({});
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setLoaded(false); setText(''); setOptions({}); setMessage(''); }, [appid]);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } catch (e) { setMessage(String(e)); }
    finally { try { await refresh(); } catch (e) { setMessage(old => old + '\n' + String(e)); } setBusy(false); }
  };
  const current = async () => {
    if (!/^[1-9][0-9]{0,9}$/.test(appid) || Number(appid) > 0xffffffff) throw new Error('Укажите Steam AppID');
    if (!account()) throw new Error('Steam не сообщает текущий аккаунт');
    return readLaunch(SteamClient.Apps, Number(appid));
  };
  const mutate = async (mode: string, preset = false) => {
    const owner = account();
    const before = await current();
    if (mode === 'write' && (!loaded || baselineKey !== owner + ':' + appid)) throw new Error('Сначала прочитайте параметры из Steam для этой игры и аккаунта');
    if (mode === 'write' && before.text !== baseline) throw new Error('Строка в Steam изменилась после чтения. Прочитайте её заново перед редактированием.');
    if (account() !== owner) throw new Error('Steam-аккаунт изменился');
    const result = unpack(await prepare(owner, appid, before.text, text, mode, preset ? options : null));
    if (mode !== 'keep') {
      const actual = await writeLaunch(SteamClient.Apps, Number(appid), before.text, result.target!, () => account() === owner);
      unpack(await finish(owner, appid, actual));
      setText(actual); setBaseline(actual);
    } else { setText(before.text); setBaseline(before.text); }
    setBaselineKey(owner + ':' + appid);
    setLoaded(true); setMessage([...result.notes, 'Параметры проверены. Изменения действуют со следующего запуска игры.'].join('\n'));
  };
  const choose = (key: string, label: string, values: [string | number | boolean, string][]) => <PanelSectionRow><DropdownItem layout="below" label={label}
    selectedOption={options[key] ?? ''} disabled={busy} rgOptions={[{ data: '', label: 'Не задавать' }, ...values.map(([data, label]) => ({ data, label }))]}
    onChange={o => setOptions(old => { const result = { ...old }; if (o.data === '') delete result[key]; else result[key] = o.data; return result; })} /></PanelSectionRow>;
  return <>
    <PanelSectionRow>Эти параметры действуют после перезапуска игры, без перезагрузки SteamOS. Графика, разрешение и HDR автоматически не меняются.</PanelSectionRow>
    <PanelSectionRow><ButtonItem layout="below" disabled={busy || !appid} onClick={() => run(async () => { const owner = account(); const value = await current(); if (account() !== owner) throw new Error('Steam-аккаунт изменился'); setText(value.text); setBaseline(value.text); setBaselineKey(owner + ':' + appid); setLoaded(true); setMessage('Прочитано из Steam'); })}>Прочитать параметры из Steam</ButtonItem></PanelSectionRow>
    {loaded && <>
      <PanelSectionRow><TextField label="Текущая строка / редактирование" value={text} onChange={e => setText(e.target.value)} /></PanelSectionRow>
      {choose('cache_gb', 'Лимит Mesa shader cache (не всех кешей Steam)', [1, 2, 4, 8, 16].map(n => [n, `${n} GiB на архитектуру`]))}
      {choose('sync', 'Синхронизация Proton', [['auto', 'Автовыбор Proton, убрать унаследованные запреты'], ['fsync', 'Обойти NTSYNC, разрешить Fsync при поддержке Proton/ядром']])}
      {choose('dxvk_latency', 'DXVK: максимум кадров в очереди', [1, 2, 3, 4].map(n => [n, String(n)]))}
      {choose('radv_wave32', 'RADV wave32 — эксперимент по игре', [['compute', 'Compute'], ['pixel', 'Pixel'], ['geometry', 'Geometry'], ['all', 'Все три стадии']])}
      {choose('radv_nir_cache', 'RADV NIR cache — только при поддержке драйвером', [[true, 'Включить']])}
      {choose('io_priority', 'I/O приоритет процесса (best-effort)', [[0, '0 — высокий'], [2, '2'], [4, '4 — обычный'], [7, '7 — низкий']])}
      {choose('affinity', 'CPU affinity игры без глобального отключения SMT', [['physical', 'Один поток на физическое ядро']])}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(async () => { const before = await current(); const result = unpack(await preview(account(), appid, before.text, options)); setText(result.target!); setMessage(result.notes.join('\n') || 'Предпросмотр готов; изменений нет'); })}>Предпросмотр выбранных параметров</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => mutate('write', true))}>Применить выбранные параметры</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => mutate('write'))}>Сохранить отредактированную строку</ButtonItem></PanelSectionRow>
    </>}
    {backups[account() + ':' + appid] && <>
      <PanelSectionRow>{backups[account() + ':' + appid].pending ? 'Есть незавершённая запись: прочитайте строку Steam и повторите применение или восстановление.' : 'Исходная строка Steam сохранена для этого аккаунта.'}</PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => mutate('restore'))}>Вернуть исходную строку Steam</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => run(() => mutate('keep'))}>Оставить внешнюю строку и снять владение</ButtonItem></PanelSectionRow>
    </>}
    {message && <PanelSectionRow><div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }}>{message}</div></PanelSectionRow>}
  </>;
}
