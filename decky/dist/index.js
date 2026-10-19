const manifest = {"name":"Bosun"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const toaster = api.toaster;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

function watchGames(client, read, sync, error) {
    let queue = Promise.resolve();
    let timer;
    let stopped = false;
    const subscriptions = [];
    const send = (game, resume = false) => {
        queue = queue.then(async () => { if (!stopped)
            await sync(game, resume); }).catch(error);
    };
    try {
        const register = client.GameSessions?.RegisterForAppLifetimeNotifications;
        if (register)
            subscriptions.push(register.call(client.GameSessions, (notification) => {
                if (timer)
                    clearTimeout(timer);
                timer = setTimeout(() => {
                    const current = read();
                    // Steam reports ID 0 for some non-Steam shortcuts; Router supplies their actual ID.
                    if (!notification.bRunning && (!notification.unAppID || current.id === String(notification.unAppID)))
                        send({ id: '', name: '' });
                    else
                        send(current.id ? current : notification.bRunning && notification.unAppID ? { id: String(notification.unAppID), name: '' } : current);
                }, 750);
            }));
        const resume = client.System?.RegisterForOnResumeFromSuspend;
        if (resume)
            subscriptions.push(resume.call(client.System, () => {
                if (timer)
                    clearTimeout(timer);
                timer = setTimeout(() => send(read(), true), 1500);
            }));
    }
    catch (e) {
        error(e);
    }
    send(read());
    return () => {
        stopped = true;
        if (timer)
            clearTimeout(timer);
        subscriptions.forEach(s => s.unregister());
    };
}

function readLaunch(apps, id) {
    return new Promise((resolve, reject) => {
        let subscription;
        let done = false;
        const timeout = setTimeout(() => { done = true; subscription?.unregister(); reject(new Error('Steam AppDetails timeout')); }, 5000);
        try {
            subscription = apps.RegisterForAppDetails(id, details => {
                if (done)
                    return;
                const shortcut = !!details.strShortcutExe;
                const text = shortcut ? details.strShortcutLaunchOptions : details.strLaunchOptions;
                if (typeof text !== 'string')
                    return;
                done = true;
                clearTimeout(timeout);
                resolve({ text, shortcut });
                subscription?.unregister();
            });
            if (done)
                subscription.unregister();
        }
        catch (error) {
            done = true;
            clearTimeout(timeout);
            subscription?.unregister();
            reject(error);
        }
    });
}
async function writeLaunch(apps, id, expected, target, sameAccount = () => true) {
    const current = await readLaunch(apps, id);
    if (!sameAccount())
        throw new Error('Steam account changed; refusing launch-option write');
    if (current.text !== expected)
        throw new Error('Steam launch options changed; refusing overwrite');
    if (current.shortcut)
        apps.SetShortcutLaunchOptions(id, target);
    else
        apps.SetAppLaunchOptions(id, target);
    for (let attempt = 0; attempt < 8; attempt++) {
        if (!sameAccount())
            throw new Error('Steam account changed; backup retained');
        const readback = await readLaunch(apps, id);
        if (readback.text === target)
            return target;
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Steam did not confirm launch options; original backup retained');
}

const preview$1 = callable('preview_launch');
const prepare = callable('prepare_launch');
const finish = callable('finish_launch');
const account = () => window.App?.m_CurrentUser?.strSteamID || '';
const unpack$1 = (r) => { if (!r.ok)
    throw new Error(r.error || 'Bosun error'); return r.data; };
async function restoreLaunchBackups(backups) {
    for (const saved of Object.values(backups)) {
        if (saved.account !== account())
            throw new Error('Для полного отката войдите в Steam-аккаунт ' + saved.account + ' и восстановите его параметры запуска');
        const current = await readLaunch(SteamClient.Apps, Number(saved.appid));
        const result = unpack$1(await prepare(saved.account, saved.appid, current.text, '', 'restore', null));
        const actual = await writeLaunch(SteamClient.Apps, Number(saved.appid), current.text, result.target, () => account() === saved.account);
        unpack$1(await finish(saved.account, saved.appid, actual));
    }
}
function LaunchPanel({ appid, backups, refresh }) {
    const [loaded, setLoaded] = SP_REACT.useState(false);
    const [text, setText] = SP_REACT.useState('');
    const [baseline, setBaseline] = SP_REACT.useState('');
    const [baselineKey, setBaselineKey] = SP_REACT.useState('');
    const [options, setOptions] = SP_REACT.useState({});
    const [message, setMessage] = SP_REACT.useState('');
    const [busy, setBusy] = SP_REACT.useState(false);
    SP_REACT.useEffect(() => { setLoaded(false); setText(''); setOptions({}); setMessage(''); }, [appid]);
    const run = async (fn) => {
        setBusy(true);
        try {
            await fn();
        }
        catch (e) {
            setMessage(String(e));
        }
        finally {
            try {
                await refresh();
            }
            catch (e) {
                setMessage(old => old + '\n' + String(e));
            }
            setBusy(false);
        }
    };
    const current = async () => {
        if (!/^[1-9][0-9]{0,9}$/.test(appid) || Number(appid) > 0xffffffff)
            throw new Error('Укажите Steam AppID');
        if (!account())
            throw new Error('Steam не сообщает текущий аккаунт');
        return readLaunch(SteamClient.Apps, Number(appid));
    };
    const mutate = async (mode, preset = false) => {
        const owner = account();
        const before = await current();
        if (mode === 'write' && (!loaded || baselineKey !== owner + ':' + appid))
            throw new Error('Сначала прочитайте параметры из Steam для этой игры и аккаунта');
        if (mode === 'write' && before.text !== baseline)
            throw new Error('Строка в Steam изменилась после чтения. Прочитайте её заново перед редактированием.');
        if (account() !== owner)
            throw new Error('Steam-аккаунт изменился');
        const result = unpack$1(await prepare(owner, appid, before.text, text, mode, preset ? options : null));
        if (mode !== 'keep') {
            const actual = await writeLaunch(SteamClient.Apps, Number(appid), before.text, result.target, () => account() === owner);
            unpack$1(await finish(owner, appid, actual));
            setText(actual);
            setBaseline(actual);
        }
        else {
            setText(before.text);
            setBaseline(before.text);
        }
        setBaselineKey(owner + ':' + appid);
        setLoaded(true);
        setMessage([...result.notes, 'Параметры проверены. Изменения действуют со следующего запуска игры.'].join('\n'));
    };
    const choose = (key, label, values) => SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: label, selectedOption: options[key] ?? '', disabled: busy, rgOptions: [{ data: '', label: 'Не задавать' }, ...values.map(([data, label]) => ({ data, label }))], onChange: o => setOptions(old => { const result = { ...old }; if (o.data === '')
                delete result[key];
            else
                result[key] = o.data; return result; }) }) });
    return SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u042D\u0442\u0438 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0434\u0435\u0439\u0441\u0442\u0432\u0443\u044E\u0442 \u043F\u043E\u0441\u043B\u0435 \u043F\u0435\u0440\u0435\u0437\u0430\u043F\u0443\u0441\u043A\u0430 \u0438\u0433\u0440\u044B, \u0431\u0435\u0437 \u043F\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0438 SteamOS. \u0413\u0440\u0430\u0444\u0438\u043A\u0430, \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043D\u0438\u0435 \u0438 HDR \u0430\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u0447\u0435\u0441\u043A\u0438 \u043D\u0435 \u043C\u0435\u043D\u044F\u044E\u0442\u0441\u044F." }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy || !appid, onClick: () => run(async () => { const owner = account(); const value = await current(); if (account() !== owner)
                        throw new Error('Steam-аккаунт изменился'); setText(value.text); setBaseline(value.text); setBaselineKey(owner + ':' + appid); setLoaded(true); setMessage('Прочитано из Steam'); }), children: "\u041F\u0440\u043E\u0447\u0438\u0442\u0430\u0442\u044C \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0438\u0437 Steam" }) }), loaded && SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "\u0422\u0435\u043A\u0443\u0449\u0430\u044F \u0441\u0442\u0440\u043E\u043A\u0430 / \u0440\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u0435", value: text, onChange: e => setText(e.target.value) }) }), choose('cache_gb', 'Лимит Mesa shader cache (не всех кешей Steam)', [1, 2, 4, 8, 16].map(n => [n, `${n} GiB на архитектуру`])), choose('sync', 'Синхронизация Proton', [['auto', 'Автовыбор Proton, убрать унаследованные запреты'], ['fsync', 'Обойти NTSYNC, разрешить Fsync при поддержке Proton/ядром']]), choose('dxvk_latency', 'DXVK: максимум кадров в очереди', [1, 2, 3, 4].map(n => [n, String(n)])), choose('radv_wave32', 'RADV wave32 — эксперимент по игре', [['compute', 'Compute'], ['pixel', 'Pixel'], ['geometry', 'Geometry'], ['all', 'Все три стадии']]), choose('radv_nir_cache', 'RADV NIR cache — только при поддержке драйвером', [[true, 'Включить']]), choose('io_priority', 'I/O приоритет процесса (best-effort)', [[0, '0 — высокий'], [2, '2'], [4, '4 — обычный'], [7, '7 — низкий']]), choose('affinity', 'CPU affinity игры без глобального отключения SMT', [['physical', 'Один поток на физическое ядро']]), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(async () => { const before = await current(); const result = unpack$1(await preview$1(account(), appid, before.text, options)); setText(result.target); setMessage(result.notes.join('\n') || 'Предпросмотр готов; изменений нет'); }), children: "\u041F\u0440\u0435\u0434\u043F\u0440\u043E\u0441\u043C\u043E\u0442\u0440 \u0432\u044B\u0431\u0440\u0430\u043D\u043D\u044B\u0445 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u043E\u0432" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => mutate('write', true)), children: "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0432\u044B\u0431\u0440\u0430\u043D\u043D\u044B\u0435 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => mutate('write')), children: "\u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C \u043E\u0442\u0440\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u043D\u043D\u0443\u044E \u0441\u0442\u0440\u043E\u043A\u0443" }) })] }), backups[account() + ':' + appid] && SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: backups[account() + ':' + appid].pending ? 'Есть незавершённая запись: прочитайте строку Steam и повторите применение или восстановление.' : 'Исходная строка Steam сохранена для этого аккаунта.' }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => mutate('restore')), children: "\u0412\u0435\u0440\u043D\u0443\u0442\u044C \u0438\u0441\u0445\u043E\u0434\u043D\u0443\u044E \u0441\u0442\u0440\u043E\u043A\u0443 Steam" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => mutate('keep')), children: "\u041E\u0441\u0442\u0430\u0432\u0438\u0442\u044C \u0432\u043D\u0435\u0448\u043D\u044E\u044E \u0441\u0442\u0440\u043E\u043A\u0443 \u0438 \u0441\u043D\u044F\u0442\u044C \u0432\u043B\u0430\u0434\u0435\u043D\u0438\u0435" }) })] }), message && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }, children: message }) })] });
}

// SPDX-License-Identifier: GPL-3.0-or-later


function Disclosure({ title, children, section = true }) {
    const [open, setOpen] = SP_REACT.useState(false);
    const content = SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => setOpen(v => !v), children: `${open ? '▾' : '▸'} ${title}` }) }), SP_JSX.jsx("div", { style: { display: open ? 'block' : 'none' }, "aria-hidden": !open, children: children })] });
    return section ? SP_JSX.jsx(DFL.PanelSection, { children: content }) : content;
}

const getState = callable('get_state');
const saveGame = callable('save_game');
const removeGame = callable('remove_game');
const activate = callable('activate');
const automatic = callable('set_automatic');
const preview = callable('preview');
const applyGlobal = callable('apply_global');
const release = callable('release');
const rollback = callable('rollback');
const update = callable('update');
const measurementTools = callable('measurement_tools');
const cpuCheck = callable('cpu_check');
const currentGame = () => ({ id: String(DFL.Router.MainRunningApp?.appid || ''), name: DFL.Router.MainRunningApp?.display_name || '' });
function unpack(reply) { if (!reply.ok)
    throw new Error(reply.error || 'Bosun error'); return reply.data; }
function notifyError(error) { toaster.toast({ title: 'Bosun', body: String(error), duration: 8000 }); }
function systemValue(key, value) {
    if (value === null)
        return 'недоступно';
    if (/^\d+$/.test(value) && key === 'cpu_max_mhz')
        return `${Number(value) / 1000} MHz`;
    if (/^\d+$/.test(value) && key === 'tdp_w')
        return `${Number(value) / 1000000} W`;
    return value.slice(0, 120);
}
async function syncGame(game, resume = false) {
    const result = unpack(await activate(game.id, resume));
    if (result.conflict || result.notes.some(n => /CONFLICT|SKIP|DIVERGENCE/.test(n)))
        toaster.toast({ title: 'Bosun: проверьте профиль', body: result.notes.join('\n').slice(0, 700), duration: 8000 });
    window.dispatchEvent(new Event('bosun:game'));
    return result;
}
const labels = {
    mitigations: 'Программные CPU mitigations', nmi_watchdog: 'NMI watchdog', zram_page_cluster: 'Swap readahead zram', mglru: 'MGLRU', thp_defrag: 'Дефрагментация THP',
    thp: 'Политика THP', swappiness: 'Swappiness', governor: 'CPU governor', epp: 'CPU EPP', boost: 'CPU Boost', smt: 'SMT', cpu_max_mhz: 'Предел CPU, MHz', tdp_w: 'TDP APU, W', gpu_mode: 'GPU policy',
    nvme_scheduler: 'NVMe I/O scheduler', sd_scheduler: 'microSD I/O scheduler', moverate: 'AMDGPU migration MB/s', lbpw: 'LBPW: неприменим к Deck RDNA2', lavd: 'Scheduler LAVD',
    compaction_proactiveness: 'Фоновая компактация RAM', nvme_readahead_kb: 'NVMe read-ahead, KiB', sd_readahead_kb: 'microSD read-ahead, KiB', zram_algorithm: 'Компрессор zram (reboot)', zram_size_percent: 'Размер zram, % RAM (reboot)', workqueue_power_efficient: 'Workqueue power efficient (reboot)',
    amd_pstate: 'Режим AMD P-state (reboot)', gpu_sched_policy: 'GPU scheduler: 0 RR / 1 FIFO (reboot)', epp_boost: 'Per-core EPP boost (если есть, reboot)', home_noatime: 'Отключить atime для /home', usb_poll_ms: 'USB HID polling, ms (reboot)', memlock_kb: 'Лимит закреплённой RAM, KiB (reboot)',
    thp_shmem: 'THP для shared memory', dirty_background_mib: 'Фоновый writeback, MiB (пара)', dirty_mib: 'Прямой writeback, MiB (пара)', sched_autogroup: 'CPU autogroup', soft_watchdog: 'Soft watchdog', ksm: 'Сканирование KSM',
};
function Content() {
    const [data, setData] = SP_REACT.useState();
    const [loadError, setLoadError] = SP_REACT.useState('');
    const [game, setGame] = SP_REACT.useState(currentGame());
    const [scope, setScope] = SP_REACT.useState('game');
    const [appid, setAppid] = SP_REACT.useState(currentGame().id);
    const [name, setName] = SP_REACT.useState(currentGame().name);
    const [draft, setDraft] = SP_REACT.useState({});
    const [dirty, setDirty] = SP_REACT.useState(false);
    const [base, setBase] = SP_REACT.useState('');
    const selection = SP_REACT.useRef('');
    const refreshSequence = SP_REACT.useRef(0);
    const [resume, setResume] = SP_REACT.useState(true);
    const [busy, setBusy] = SP_REACT.useState(false);
    const [message, setMessage] = SP_REACT.useState('');
    const [cpuMessage, setCpuMessage] = SP_REACT.useState('');
    const [key, setKey] = SP_REACT.useState('compaction_proactiveness');
    const [group, setGroup] = SP_REACT.useState('Все');
    const [value, setValue] = SP_REACT.useState('0');
    const refresh = async () => {
        const sequence = ++refreshSequence.current;
        try {
            const next = unpack(await getState());
            if (sequence === refreshSequence.current) {
                setData(next);
                setLoadError('');
            }
        }
        catch (e) {
            if (sequence === refreshSequence.current)
                setLoadError(String(e));
            throw e;
        }
    };
    SP_REACT.useEffect(() => {
        refresh().catch(notifyError);
        const changed = () => { setGame(currentGame()); refresh().catch(notifyError); };
        window.addEventListener('bosun:game', changed);
        return () => { ++refreshSequence.current; window.removeEventListener('bosun:game', changed); };
    }, []);
    SP_REACT.useEffect(() => {
        if (!data)
            return;
        const target = scope === 'global' ? 'global' : 'game:' + appid;
        if (selection.current === target && dirty)
            return;
        if (selection.current !== target) {
            setBase('');
            setMessage('');
        }
        selection.current = target;
        const saved = data.config.games[appid];
        setDraft(scope === 'global' ? { ...data.report.desired } : { ...(saved?.settings || {}) });
        if (scope === 'game') {
            setName(saved?.name || (appid === game.id ? game.name : ''));
            setResume(saved?.resume ?? true);
        }
        setDirty(false);
    }, [data, scope, appid, dirty, game.id, game.name]);
    const run = async (action, commit = false) => {
        setBusy(true);
        const target = selection.current;
        let committed = false;
        try {
            const result = unpack(await action());
            setMessage(result.notes.join('\n') || 'Готово');
            committed = commit && !result.conflict;
        }
        catch (e) {
            setMessage(String(e));
        }
        finally {
            try {
                await refresh();
                if (committed && selection.current === target)
                    setDirty(false);
            }
            catch (e) {
                setMessage(old => old + '\n' + String(e));
            }
            setBusy(false);
        }
    };
    const set = (setting, next) => {
        setDirty(true);
        setMessage('');
        setDraft(old => {
            const result = { ...old };
            if (next === undefined || next === '')
                delete result[setting];
            else
                result[setting] = next;
            if (next === undefined && ['dirty_background_mib', 'dirty_mib'].includes(setting)) {
                delete result.dirty_background_mib;
                delete result.dirty_mib;
            }
            if (setting === 'epp' && next)
                result.governor = 'powersave';
            if (setting === 'governor' && next !== 'powersave')
                delete result.epp;
            return result;
        });
    };
    const choose = (setting, options) => SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: labels[setting], selectedOption: draft[setting] ?? '', disabled: busy, rgOptions: [{ data: '', label: 'Наследовать / не менять' }, ...options.map(o => ({ data: o, label: o }))], onChange: o => set(setting, String(o.data)) }) });
    const slider = (setting, min, max, step, initial) => SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: `Управлять: ${labels[setting]}`, checked: draft[setting] !== undefined, disabled: busy, onChange: on => set(setting, on ? initial : undefined) }) }), draft[setting] !== undefined && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.SliderField, { layout: "below", label: labels[setting], value: Number(draft[setting]), min: min, max: max, step: step, showValue: true, editableValue: true, disabled: busy, onChange: v => set(setting, v) }) })] });
    if (!data)
        return SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: loadError || 'Загрузка Bosun…' }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => refresh().catch(notifyError), children: "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0443" }) })] });
    const system = data.report.system;
    const savedApps = [...new Set([...Object.keys(data.config.games), ...Object.values(data.config.launch_options || {}).map(saved => saved.appid)])];
    const allowed = Object.keys(data.catalog).filter(k => k !== 'lbpw' && (scope === 'global' || data.runtime_keys.includes(k)));
    const keys = allowed.filter(k => group === 'Все' || (data.groups?.[k] || 'Дополнительно') === group);
    const selectedKey = keys.includes(key) ? key : keys[0] || '';
    const bases = scope === 'global' ? data.profiles : data.game_profiles || Object.fromEntries(Object.entries(data.profiles)
        .map(([p, s]) => [p, Object.fromEntries(Object.entries(s).filter(([k]) => data.runtime_keys.includes(k)))])
        .filter(([p, s]) => p === 'stock' || Object.keys(s).length));
    const add = () => {
        if (!keys.includes(selectedKey)) {
            setMessage('Выберите допустимую настройку');
            return;
        }
        const parsed = /^-?\d+$/.test(value) ? Number(value) : value;
        set(selectedKey, parsed);
        setMessage('Параметр добавлен в черновик. Проверьте предпросмотр.');
    };
    return SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSection, { title: `Bosun 0.4.2 · ${system.model || 'Steam Deck'}`, children: [SP_JSX.jsxs(DFL.PanelSectionRow, { children: [game.id ? `${game.name} (${game.id})` : 'Игра не запущена', SP_JSX.jsx("br", {}), "\u041F\u0440\u043E\u0444\u0438\u043B\u044C \u0443\u0441\u0442\u0440\u043E\u0439\u0441\u0442\u0432\u0430: ", data.report.profile] }), data.report.update_pending && SP_JSX.jsx(DFL.PanelSectionRow, { children: "SteamOS \u043E\u0431\u043D\u043E\u0432\u0438\u043B\u0430\u0441\u044C: \u043F\u0440\u043E\u0432\u0435\u0440\u044C\u0442\u0435 \u0438 \u044F\u0432\u043D\u043E \u043F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u0435 \u0433\u043B\u043E\u0431\u0430\u043B\u044C\u043D\u044B\u0439 \u043F\u0440\u043E\u0444\u0438\u043B\u044C." }), data.power_conflicts.length > 0 && SP_JSX.jsxs(DFL.PanelSectionRow, { children: ["Power-\u043D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u043F\u0440\u043E\u043F\u0443\u0441\u043A\u0430\u044E\u0442\u0441\u044F: ", data.power_conflicts.join(', '), "."] }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "\u0420\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u0442\u044C", selectedOption: scope, disabled: busy, rgOptions: [{ data: 'game', label: 'Игра · на лету' }, { data: 'global', label: 'Устройство · глобально' }], onChange: o => { setScope(String(o.data)); setGroup('Все'); } }) }), scope === 'game' && SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSectionRow, { children: ["\u0412\u044B\u0431\u0440\u0430\u043D\u043E: ", name || 'Без названия', " ", appid ? `(${appid})` : '· укажите AppID'] }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => setAppid(game.id), children: "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u0437\u0430\u043F\u0443\u0449\u0435\u043D\u043D\u0443\u044E \u0438\u0433\u0440\u0443" }) }), SP_JSX.jsxs(Disclosure, { title: "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u0434\u0440\u0443\u0433\u0443\u044E \u0438\u0433\u0440\u0443", section: false, children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "\u0421\u043E\u0445\u0440\u0430\u043D\u0451\u043D\u043D\u044B\u0435 \u0438\u0433\u0440\u044B \u0438 \u0441\u0442\u0440\u043E\u043A\u0438 \u0437\u0430\u043F\u0443\u0441\u043A\u0430", selectedOption: appid, disabled: busy, rgOptions: savedApps.map(id => ({ data: id, label: `${data.config.games[id]?.name || 'Steam'} (${id})` })), onChange: o => setAppid(String(o.data)) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "AppID (\u043C\u043E\u0436\u043D\u043E \u0443\u043A\u0430\u0437\u0430\u0442\u044C \u0434\u043E \u0437\u0430\u043F\u0443\u0441\u043A\u0430 \u0438\u0433\u0440\u044B)", value: appid, disabled: busy, onChange: e => setAppid(e.target.value) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "\u041D\u0430\u0437\u0432\u0430\u043D\u0438\u0435", value: name, disabled: busy, onChange: e => { setName(e.target.value); setDirty(true); } }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "\u041E\u0434\u0438\u043D \u0440\u0430\u0437 \u0432\u043E\u0441\u0441\u0442\u0430\u043D\u043E\u0432\u0438\u0442\u044C \u043F\u043E\u0441\u043B\u0435 \u0441\u043D\u0430", checked: resume, disabled: busy, onChange: v => { setResume(v); setDirty(true); } }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy || !appid, onClick: () => run(() => removeGame(appid), true), children: "\u0423\u0434\u0430\u043B\u0438\u0442\u044C \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435" }) })] })] })] }), SP_JSX.jsxs(DFL.PanelSection, { title: scope === 'game' ? 'Настройки игры · без перезагрузки' : 'Общий профиль устройства', children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: dirty ? 'Есть несохранённые изменения.' : 'Показаны сохранённые настройки.' }), scope === 'game' && SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u0417\u0434\u0435\u0441\u044C \u0442\u043E\u043B\u044C\u043A\u043E \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u043D\u0430 \u043B\u0435\u0442\u0443. \u0417\u0430\u0433\u0440\u0443\u0437\u043E\u0447\u043D\u044B\u0435 \u043D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u2014 \u0432 \u0440\u0435\u0436\u0438\u043C\u0435 \u00AB\u0423\u0441\u0442\u0440\u043E\u0439\u0441\u0442\u0432\u043E\u00BB." }), scope === 'game' && !data.config.automatic && SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u0410\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u0447\u0435\u0441\u043A\u0438\u0435 \u043F\u0440\u043E\u0444\u0438\u043B\u0438 \u0432\u044B\u043A\u043B\u044E\u0447\u0435\u043D\u044B: \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435 \u0441\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u0441\u044F \u0431\u0435\u0437 \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u0438\u044F. \u0412\u043A\u043B\u044E\u0447\u0438\u0442\u0435 \u0438\u0445 \u0432 \u0440\u0430\u0437\u0434\u0435\u043B\u0435 \u00AB\u0410\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u043A\u0430 \u0438 \u0443\u0441\u0442\u0440\u043E\u0439\u0441\u0442\u0432\u043E\u00BB." }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u043E\u0441\u043D\u043E\u0432\u0443 \u0432 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A", selectedOption: base, disabled: busy, strDefaultLabel: "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u043F\u0440\u043E\u0444\u0438\u043B\u044C", rgOptions: Object.keys(bases).map(p => ({ data: p, label: p })), onChange: o => { const settings = bases[String(o.data)]; setBase(String(o.data)); setDirty(true); setMessage(''); setDraft({ ...settings }); } }) }), slider('cpu_max_mhz', 400, 3500, 100, 2800), slider('tdp_w', 4, 15, 1, 10), SP_JSX.jsxs(Disclosure, { title: "CPU \u0438 GPU: \u043F\u043E\u043B\u0438\u0442\u0438\u043A\u0430", section: false, children: [choose('governor', ['powersave', 'performance', 'schedutil']), choose('epp', ['performance', 'balance_performance', 'balance_power', 'power']), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "CPU Boost", selectedOption: draft.boost ?? '', disabled: busy, rgOptions: [{ data: '', label: 'Наследовать' }, { data: 0, label: 'Выключить' }, { data: 1, label: 'Включить' }], onChange: o => set('boost', o.data === '' ? undefined : Number(o.data)) }) }), choose('gpu_mode', ['auto', 'low', 'high'])] }), SP_JSX.jsx(DFL.PanelSectionRow, { children: "FPS cap \u0438 refresh \u0437\u0430\u0434\u0430\u044E\u0442\u0441\u044F \u0441\u0440\u0435\u0434\u0441\u0442\u0432\u0430\u043C\u0438 Steam. \u0413\u0440\u0430\u0444\u0438\u043A\u0430 \u0438 \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043D\u0438\u0435 \u043D\u0435 \u0441\u043D\u0438\u0436\u0430\u044E\u0442\u0441\u044F." }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => preview(draft, scope === 'game')), children: "\u041F\u0440\u0435\u0434\u043F\u0440\u043E\u0441\u043C\u043E\u0442\u0440 \u0431\u0435\u0437 \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u0439" }) }), scope === 'game' ? SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy || !appid, onClick: () => run(async () => {
                                const result = unpack(await saveGame(appid, name, draft, resume));
                                if (appid === currentGame().id && data.config.automatic) {
                                    const applied = await syncGame(currentGame());
                                    return { ok: true, data: { notes: [...result.notes, ...applied.notes], conflict: applied.conflict } };
                                }
                                return { ok: true, data: { notes: [...result.notes, data.config.automatic ? 'Назначение применится при запуске этой игры.' : 'SKIP: автоматические профили выключены; назначение сохранено без применения.'] } };
                            }, true), children: appid === game.id && data.config.automatic ? 'Сохранить и применить к текущей игре' : 'Сохранить назначение игре' }) }) : SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => applyGlobal('decky-global', draft), true), children: "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0433\u043B\u043E\u0431\u0430\u043B\u044C\u043D\u043E" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => applyGlobal('recommended', data.profiles.recommended), true), children: "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0440\u0435\u043A\u043E\u043C\u0435\u043D\u0434\u0443\u0435\u043C\u044B\u0439 \u043F\u0440\u043E\u0444\u0438\u043B\u044C" }) })] }), dirty && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => { setBase(''); setDirty(false); }, children: "\u0421\u0431\u0440\u043E\u0441\u0438\u0442\u044C \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A \u043A \u0441\u043E\u0445\u0440\u0430\u043D\u0451\u043D\u043D\u043E\u043C\u0443 \u043F\u0440\u043E\u0444\u0438\u043B\u044E" }) }), message && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }, children: message }) })] }), SP_JSX.jsxs(Disclosure, { title: "\u0414\u043E\u043F\u043E\u043B\u043D\u0438\u0442\u0435\u043B\u044C\u043D\u044B\u0435 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0438 \u044D\u043A\u0441\u043F\u0435\u0440\u0438\u043C\u0435\u043D\u0442\u044B", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "\u0413\u0440\u0443\u043F\u043F\u0430 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u043E\u0432", selectedOption: group, disabled: busy, rgOptions: ['Все', ...new Set(allowed.map(k => data.groups?.[k] || 'Дополнительно'))].map(g => ({ data: g, label: g })), onChange: o => setGroup(String(o.data)) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { layout: "below", label: "\u041F\u0430\u0440\u0430\u043C\u0435\u0442\u0440", selectedOption: selectedKey, disabled: busy, rgOptions: keys.map(k => ({ data: k, label: data.labels?.[k] || labels[k] || k })), onChange: o => setKey(String(o.data)) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: 12 }, children: data.catalog[selectedKey]?.[1] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: `Значение: ${data.catalog[selectedKey]?.[0] || ''}`, value: value, disabled: busy, onChange: e => setValue(e.target.value) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: add, children: "\u0414\u043E\u0431\u0430\u0432\u0438\u0442\u044C / \u0437\u0430\u043C\u0435\u043D\u0438\u0442\u044C \u0432 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A\u0435" }) }), SP_JSX.jsxs(DFL.PanelSectionRow, { children: ["\u0412 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A\u0435: ", Object.keys(draft).length, ". \u0423\u0434\u0430\u043B\u0435\u043D\u0438\u0435 \u043D\u0438\u0436\u0435 \u0432\u043E\u0437\u0432\u0440\u0430\u0449\u0430\u0435\u0442 \u043D\u0430\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u043F\u0440\u0438 \u0441\u043B\u0435\u0434\u0443\u044E\u0449\u0435\u043C \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u0438\u0438."] }), Object.entries(draft).map(([k, v]) => SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => set(k, undefined), children: [data.labels?.[k] || labels[k] || k, ": ", v, " \u00B7 \u0443\u0431\u0440\u0430\u0442\u044C"] }) }, k))] }), scope === 'game' && SP_JSX.jsx(Disclosure, { title: "\u041F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0437\u0430\u043F\u0443\u0441\u043A\u0430 Steam \u00B7 \u0441\u043E \u0441\u043B\u0435\u0434\u0443\u044E\u0449\u0435\u0433\u043E \u0437\u0430\u043F\u0443\u0441\u043A\u0430 \u0438\u0433\u0440\u044B", children: SP_JSX.jsx(LaunchPanel, { appid: appid, backups: data.config.launch_options || {}, refresh: refresh }) }), SP_JSX.jsxs(Disclosure, { title: "\u0424\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u043E\u0435 \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435 \u0443\u0441\u0442\u0440\u043E\u0439\u0441\u0442\u0432\u0430", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u041F\u043E\u0441\u043B\u0435\u0434\u043D\u0435\u0435 \u0447\u0442\u0435\u043D\u0438\u0435 \u0438\u0437 \u0441\u0438\u0441\u0442\u0435\u043C\u044B; \u0432\u044B\u0431\u0440\u0430\u043D\u043D\u044B\u0435 \u0437\u043D\u0430\u0447\u0435\u043D\u0438\u044F \u0432 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A\u0435 \u0441\u0430\u043C\u0438 \u043F\u043E \u0441\u0435\u0431\u0435 \u043D\u0435 \u043F\u043E\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u044E\u0442 \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u0438\u0435." }), Object.entries(system.cpu).map(([p, cpu]) => SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: 13, overflowWrap: 'anywhere' }, children: [p.split('/').pop(), " \u00B7 ", cpu.scaling_driver, " \u00B7 ", cpu.scaling_governor, SP_JSX.jsx("br", {}), "\u0414\u0438\u0430\u043F\u0430\u0437\u043E\u043D CPUFreq: ", systemValue('cpu_max_mhz', cpu.scaling_min_freq ?? null), " \u2014 ", systemValue('cpu_max_mhz', cpu.scaling_max_freq ?? null), SP_JSX.jsx("br", {}), "\u0422\u0435\u043A\u0443\u0449\u0430\u044F (", cpu.cpuinfo_cur_freq ? 'cpuinfo_cur_freq' : 'scaling_cur_freq', "): ", systemValue('cpu_max_mhz', cpu.cpuinfo_cur_freq || cpu.scaling_cur_freq || null)] }) }, p)), SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u0421\u043E\u0432\u043F\u0430\u0434\u0435\u043D\u0438\u0435 CPUFreq \u0441 \u043D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u043E\u0439 \u043F\u043E\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u0435\u0442 \u0437\u0430\u043F\u0438\u0441\u044C policy, \u043D\u043E \u043D\u0435 \u0430\u043F\u043F\u0430\u0440\u0430\u0442\u043D\u044B\u0439 \u043F\u043E\u0442\u043E\u043B\u043E\u043A Boost. \u041F\u0440\u0438 \u043F\u0440\u0435\u0432\u044B\u0448\u0435\u043D\u0438\u0438 \u0432 \u043E\u0432\u0435\u0440\u043B\u0435\u0435 \u0432\u044B\u043F\u043E\u043B\u043D\u0438\u0442\u0435 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0443 \u043D\u0438\u0436\u0435." }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(async () => {
                                const result = unpack(await cpuCheck());
                                setCpuMessage(result.notes.join('\n'));
                                return { ok: true, data: { notes: ['Результат проверки CPU — в блоке фактического состояния.'] } };
                            }), children: "\u041F\u0440\u043E\u0432\u0435\u0440\u0438\u0442\u044C CPU: \u043B\u0438\u043C\u0438\u0442\u044B \u0438 \u0447\u0430\u0441\u0442\u043E\u0442\u044B (2 \u0441)" }) }), cpuMessage && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, children: cpuMessage }) }), data.report.managed.map((m, i) => SP_JSX.jsxs(DFL.PanelSectionRow, { children: [SP_JSX.jsxs("div", { style: { fontSize: 13, overflowWrap: 'anywhere' }, children: [m.change.path?.endsWith('/scaling_min_freq') ? 'Нижняя граница CPU (связана с пределом)' : labels[m.change.key] || m.change.key, ": ", ['boot', 'zram_config'].includes(m.change.key) ? 'override сохранён; проверка эффекта после reboot' : SP_JSX.jsxs(SP_JSX.Fragment, { children: ["\u043E\u0436\u0438\u0434\u0430\u043B\u043E\u0441\u044C ", systemValue(m.change.key, m.last), "; \u043F\u0440\u043E\u0447\u0438\u0442\u0430\u043D\u043E ", systemValue(m.change.key, m.actual), " ", m.matches ? '✓ совпадает' : '— расхождение'] })] }), !m.matches && m.change.kind === 'scalar' && SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => release(m.change.key)), children: "\u041E\u0441\u0442\u0430\u0432\u0438\u0442\u044C \u0444\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u043E\u0435 \u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435" })] }, i)), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => refresh().catch(notifyError), children: "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C \u0444\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u043E\u0435 \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435" }) })] }), SP_JSX.jsxs(Disclosure, { title: "\u0410\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u043A\u0430 \u0438 \u0443\u0441\u0442\u0440\u043E\u0439\u0441\u0442\u0432\u043E", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "\u0410\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u0447\u0435\u0441\u043A\u0438\u0435 \u043F\u0440\u043E\u0444\u0438\u043B\u0438 \u0438\u0433\u0440", checked: data.config.automatic, disabled: busy, onChange: enabled => run(async () => {
                                const result = unpack(await automatic(enabled));
                                const applied = await syncGame(currentGame());
                                return { ok: true, data: { notes: [...result.notes, ...applied.notes], conflict: applied.conflict } };
                            }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(async () => ({ ok: true, data: await syncGame(currentGame()) })), children: "\u0421\u0438\u043D\u0445\u0440\u043E\u043D\u0438\u0437\u0438\u0440\u043E\u0432\u0430\u0442\u044C \u0442\u0435\u043A\u0443\u0449\u0443\u044E \u0438\u0433\u0440\u0443" }) }), SP_JSX.jsxs(DFL.PanelSectionRow, { children: ["\u042F\u0434\u0440\u043E: ", system.kernel, SP_JSX.jsx("br", {}), "CPU driver: ", [...new Set(Object.values(system.cpu).map(p => p.scaling_driver))].join(', '), SP_JSX.jsx("br", {}), "RAM ", system.model_specs.ram_mt_s, " MT/s \u00B7 \u0431\u0430\u0442\u0430\u0440\u0435\u044F ", system.model_specs.battery_design_wh, " Wh \u00B7 \u043F\u0430\u043D\u0435\u043B\u044C \u0434\u043E ", system.model_specs.panel_max_hz, " Hz \u00B7 RO ", system.readonly] })] }), SP_JSX.jsxs(Disclosure, { title: "\u041E\u0431\u0441\u043B\u0443\u0436\u0438\u0432\u0430\u043D\u0438\u0435 \u0438 \u043E\u0442\u043A\u0430\u0442", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(measurementTools), children: "\u041F\u043E\u0434\u0433\u043E\u0442\u043E\u0432\u0438\u0442\u044C MangoHud \u0434\u043B\u044F A/B-\u0437\u0430\u043C\u0435\u0440\u043E\u0432" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(() => update()), children: "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C Bosun \u0438 \u043F\u043B\u0430\u0433\u0438\u043D" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(async () => { await restoreLaunchBackups(data.config.launch_options || {}); return rollback(); }, true), children: "\u041F\u043E\u043B\u043D\u044B\u0439 \u043E\u0442\u043A\u0430\u0442, \u0432\u043A\u043B\u044E\u0447\u0430\u044F \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0437\u0430\u043F\u0443\u0441\u043A\u0430 Steam" }) })] })] });
}
var index = definePlugin(() => {
    const stop = watchGames(SteamClient, currentGame, async (game, resume) => { await syncGame(game, resume); }, notifyError);
    // Decky otherwise unmounts the form when QAM hides, losing pending native dropdown callbacks and focus targets.
    return { name: 'Bosun', alwaysRender: true, titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "Bosun" }), content: SP_JSX.jsx(Content, {}), icon: SP_JSX.jsx("span", { children: "\u2693" }), onDismount: stop };
});

export { index as default };
