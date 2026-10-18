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
const currentGame = () => ({ id: String(DFL.Router.MainRunningApp?.appid || ''), name: DFL.Router.MainRunningApp?.display_name || '' });
function unpack(reply) { if (!reply.ok)
    throw new Error(reply.error || 'Bosun error'); return reply.data; }
function notifyError(error) { toaster.toast({ title: 'Bosun', body: String(error), duration: 8000 }); }
async function syncGame(game, resume = false) {
    const result = unpack(await activate(game.id, resume));
    if (result.conflict || result.notes.some(n => /CONFLICT|SKIP|DIVERGENCE/.test(n)))
        toaster.toast({ title: 'Bosun: проверьте профиль', body: result.notes.join('\n').slice(0, 700), duration: 8000 });
    window.dispatchEvent(new Event('bosun:game'));
}
const labels = {
    mitigations: 'Программные CPU mitigations', nmi_watchdog: 'NMI watchdog', zram_page_cluster: 'Swap readahead zram', mglru: 'MGLRU', thp_defrag: 'Дефрагментация THP',
    thp: 'Политика THP', swappiness: 'Swappiness', governor: 'CPU governor', epp: 'CPU EPP', boost: 'CPU Boost', smt: 'SMT', cpu_max_mhz: 'Предел CPU, MHz', tdp_w: 'TDP APU, W', gpu_mode: 'GPU policy',
    nvme_scheduler: 'NVMe I/O scheduler', sd_scheduler: 'microSD I/O scheduler', moverate: 'AMDGPU migration MB/s', lbpw: 'LBPW: неприменим к Deck RDNA2', lavd: 'Scheduler LAVD',
    compaction_proactiveness: 'Фоновая компактация RAM', nvme_readahead_kb: 'NVMe read-ahead, KiB', sd_readahead_kb: 'microSD read-ahead, KiB', zram_algorithm: 'Компрессор zram (reboot)', zram_size_percent: 'Размер zram, % RAM (reboot)', workqueue_power_efficient: 'Workqueue power efficient (reboot)',
};
function Content() {
    const [data, setData] = SP_REACT.useState();
    const [loadError, setLoadError] = SP_REACT.useState('');
    const [game, setGame] = SP_REACT.useState(currentGame());
    const [scope, setScope] = SP_REACT.useState('game');
    const [appid, setAppid] = SP_REACT.useState(currentGame().id);
    const [name, setName] = SP_REACT.useState(currentGame().name);
    const [draft, setDraft] = SP_REACT.useState({});
    const [resume, setResume] = SP_REACT.useState(true);
    const [busy, setBusy] = SP_REACT.useState(false);
    const [message, setMessage] = SP_REACT.useState('');
    const [key, setKey] = SP_REACT.useState('compaction_proactiveness');
    const [value, setValue] = SP_REACT.useState('0');
    const refresh = async () => { try {
        setData(unpack(await getState()));
        setLoadError('');
    }
    catch (e) {
        setLoadError(String(e));
        throw e;
    } };
    SP_REACT.useEffect(() => {
        refresh().catch(notifyError);
        const changed = () => { setGame(currentGame()); refresh().catch(notifyError); };
        window.addEventListener('bosun:game', changed);
        return () => window.removeEventListener('bosun:game', changed);
    }, []);
    SP_REACT.useEffect(() => {
        if (!data)
            return;
        const saved = data.config.games[appid];
        setDraft(scope === 'global' ? { ...data.report.desired } : { ...(saved?.settings || {}) });
        if (saved) {
            setName(saved.name);
            setResume(saved.resume);
        }
    }, [data, scope, appid]);
    const run = async (action) => {
        setBusy(true);
        try {
            const result = unpack(await action());
            setMessage(result.notes.join('\n') || 'Готово');
            await refresh();
        }
        catch (e) {
            setMessage(String(e));
        }
        finally {
            setBusy(false);
        }
    };
    const set = (setting, next) => setDraft(old => {
        const result = { ...old };
        if (next === undefined || next === '')
            delete result[setting];
        else
            result[setting] = next;
        if (setting === 'epp' && next)
            result.governor = 'powersave';
        if (setting === 'governor' && next !== 'powersave')
            delete result.epp;
        return result;
    });
    const choose = (setting, options) => SP_JSX.jsx(DFL.DropdownItem, { label: labels[setting], selectedOption: draft[setting] ?? '', disabled: busy, rgOptions: [{ data: '', label: 'Наследовать / не менять' }, ...options.map(o => ({ data: o, label: o }))], onChange: o => set(setting, String(o.data)) });
    const slider = (setting, min, max, step, initial) => SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.ToggleField, { label: `Управлять: ${labels[setting]}`, checked: draft[setting] !== undefined, disabled: busy, onChange: on => set(setting, on ? initial : undefined) }), draft[setting] !== undefined && SP_JSX.jsx(DFL.SliderField, { label: labels[setting], value: Number(draft[setting]), min: min, max: max, step: step, showValue: true, editableValue: true, disabled: busy, onChange: v => set(setting, v) })] });
    if (!data)
        return SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: loadError || 'Загрузка Bosun…' }), SP_JSX.jsx(DFL.ButtonItem, { onClick: () => refresh().catch(notifyError), children: "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0443" })] });
    const system = data.report.system;
    const keys = Object.keys(data.catalog).filter(k => k !== 'lbpw' && (scope === 'global' || data.runtime_keys.includes(k)));
    const add = () => {
        if (!keys.includes(key)) {
            setMessage('Выберите допустимую настройку');
            return;
        }
        const parsed = /^-?\d+$/.test(value) ? Number(value) : value;
        set(key, parsed);
        setMessage('Параметр добавлен в черновик. Проверьте предпросмотр.');
    };
    return SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSection, { title: `Bosun 0.2 · ${system.model || 'неизвестная модель'}`, children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: 13 }, children: ["RAM ", system.model_specs.ram_mt_s, " MT/s \u00B7 \u0431\u0430\u0442\u0430\u0440\u0435\u044F ", system.model_specs.battery_design_wh, " Wh \u00B7 \u043F\u0430\u043D\u0435\u043B\u044C \u0434\u043E ", system.model_specs.panel_max_hz, " Hz", SP_JSX.jsx("br", {}), "\u042F\u0434\u0440\u043E ", system.kernel, SP_JSX.jsx("br", {}), "CPU driver: ", [...new Set(Object.values(system.cpu).map(p => p.scaling_driver))].join(', '), SP_JSX.jsx("br", {}), "\u041F\u0440\u043E\u0444\u0438\u043B\u044C: ", data.report.profile, " \u00B7 RO: ", system.readonly, SP_JSX.jsx("br", {}), game.id ? `Игра: ${game.name} (${game.id})` : 'Игра не запущена'] }) }), data.report.update_pending && SP_JSX.jsx(DFL.PanelSectionRow, { children: "SteamOS/\u0441\u0442\u0435\u043A \u043E\u0431\u043D\u043E\u0432\u0438\u043B\u0441\u044F: \u043F\u0440\u043E\u0432\u0435\u0440\u044C\u0442\u0435 \u0433\u043B\u043E\u0431\u0430\u043B\u044C\u043D\u044B\u0439 \u043F\u0440\u043E\u0444\u0438\u043B\u044C \u043F\u0435\u0440\u0435\u0434 \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u0438\u0435\u043C." }), data.power_conflicts.length > 0 && SP_JSX.jsxs(DFL.PanelSectionRow, { children: ["\u041A\u043E\u043D\u043A\u0443\u0440\u0438\u0440\u0443\u044E\u0449\u0438\u0439 \u043C\u0435\u043D\u0435\u0434\u0436\u0435\u0440 \u043F\u0438\u0442\u0430\u043D\u0438\u044F: ", data.power_conflicts.join(', '), ". Power-\u043D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u0431\u0443\u0434\u0443\u0442 \u043F\u0440\u043E\u043F\u0443\u0449\u0435\u043D\u044B."] }), SP_JSX.jsx(DFL.ToggleField, { label: "\u0410\u0432\u0442\u043E\u043C\u0430\u0442\u0438\u0447\u0435\u0441\u043A\u0438\u0435 \u043F\u0440\u043E\u0444\u0438\u043B\u0438 \u0438\u0433\u0440", checked: data.config.automatic, disabled: busy, onChange: enabled => run(async () => { const r = await automatic(enabled); unpack(r); await syncGame(currentGame()); return r; }) }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(async () => { await syncGame(currentGame()); return { ok: true, data: { notes: ['Текущая игра синхронизирована'] } }; }), children: "\u0421\u0438\u043D\u0445\u0440\u043E\u043D\u0438\u0437\u0438\u0440\u043E\u0432\u0430\u0442\u044C \u0442\u0435\u043A\u0443\u0449\u0443\u044E \u0438\u0433\u0440\u0443" }), SP_JSX.jsx(DFL.DropdownItem, { label: "\u0420\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u0442\u044C", selectedOption: scope, rgOptions: [{ data: 'game', label: 'Профиль игры' }, { data: 'global', label: 'Глобальные настройки' }], onChange: o => setScope(String(o.data)) })] }), scope === 'game' && SP_JSX.jsxs(DFL.PanelSection, { title: "\u041D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435 \u0438\u0433\u0440\u0435", children: [SP_JSX.jsx(DFL.ButtonItem, { onClick: () => { setAppid(game.id); setName(game.name); }, children: "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u0437\u0430\u043F\u0443\u0449\u0435\u043D\u043D\u0443\u044E \u0438\u0433\u0440\u0443" }), SP_JSX.jsx(DFL.DropdownItem, { label: "\u0421\u043E\u0445\u0440\u0430\u043D\u0451\u043D\u043D\u044B\u0435 \u0438\u0433\u0440\u044B", selectedOption: appid, rgOptions: Object.entries(data.config.games).map(([id, p]) => ({ data: id, label: `${p.name || id} (${id})` })), onChange: o => setAppid(String(o.data)) }), SP_JSX.jsx(DFL.TextField, { label: "AppID (\u043C\u043E\u0436\u043D\u043E \u0443\u043A\u0430\u0437\u0430\u0442\u044C \u0434\u043E \u0437\u0430\u043F\u0443\u0441\u043A\u0430 \u0438\u0433\u0440\u044B)", value: appid, onChange: e => setAppid(e.target.value) }), SP_JSX.jsx(DFL.TextField, { label: "\u041D\u0430\u0437\u0432\u0430\u043D\u0438\u0435", value: name, onChange: e => setName(e.target.value) }), SP_JSX.jsx(DFL.ToggleField, { label: "\u041E\u0434\u0438\u043D \u0440\u0430\u0437 \u0432\u043E\u0441\u0441\u0442\u0430\u043D\u043E\u0432\u0438\u0442\u044C \u043F\u043E\u0441\u043B\u0435 \u0441\u043D\u0430", description: "\u0422\u043E\u043B\u044C\u043A\u043E \u0438\u0437\u0432\u0435\u0441\u0442\u043D\u044B\u0435 \u0441\u0431\u0440\u043E\u0441\u044B; \u0431\u0435\u0437 \u043F\u043E\u0441\u0442\u043E\u044F\u043D\u043D\u043E\u0439 \u043F\u0435\u0440\u0435\u0437\u0430\u043F\u0438\u0441\u0438 \u043D\u0430\u0441\u0442\u0440\u043E\u0435\u043A Steam", checked: resume, onChange: setResume })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "\u041D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u043F\u0440\u043E\u0444\u0438\u043B\u044F", children: [SP_JSX.jsx(DFL.DropdownItem, { label: "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u043E\u0441\u043D\u043E\u0432\u0443 \u0432 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A", selectedOption: "", strDefaultLabel: "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u043F\u0440\u043E\u0444\u0438\u043B\u044C", rgOptions: Object.keys(data.profiles).map(p => ({ data: p, label: p })), onChange: o => { const settings = data.profiles[String(o.data)]; setDraft(Object.fromEntries(Object.entries(settings).filter(([k]) => scope === 'global' || data.runtime_keys.includes(k)))); } }), choose('governor', ['powersave', 'performance', 'schedutil']), choose('epp', ['performance', 'balance_performance', 'balance_power', 'power']), SP_JSX.jsx(DFL.DropdownItem, { label: "CPU Boost", selectedOption: draft.boost ?? '', rgOptions: [{ data: '', label: 'Наследовать' }, { data: 0, label: 'Выключить' }, { data: 1, label: 'Включить' }], onChange: o => set('boost', o.data === '' ? undefined : Number(o.data)) }), slider('cpu_max_mhz', 400, 3500, 100, 2800), slider('tdp_w', 4, 15, 1, 10), choose('gpu_mode', ['auto', 'low', 'high']), SP_JSX.jsx(DFL.PanelSectionRow, { children: "\u0417\u0430\u0434\u0430\u0439\u0442\u0435 FPS cap \u0438 refresh \u0448\u0442\u0430\u0442\u043D\u044B\u043C\u0438 \u0441\u0440\u0435\u0434\u0441\u0442\u0432\u0430\u043C\u0438 Steam; \u0433\u0440\u0430\u0444\u0438\u043A\u0430 \u0438 \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043D\u0438\u0435 \u044D\u0442\u0438\u043C \u043F\u0440\u043E\u0444\u0438\u043B\u0435\u043C \u043D\u0435 \u0441\u043D\u0438\u0436\u0430\u044E\u0442\u0441\u044F." })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "\u041E\u0442\u0434\u0435\u043B\u044C\u043D\u044B\u0435 \u0438 \u044D\u043A\u0441\u043F\u0435\u0440\u0438\u043C\u0435\u043D\u0442\u0430\u043B\u044C\u043D\u044B\u0435 \u043F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B", children: [SP_JSX.jsx(DFL.DropdownItem, { label: "\u041F\u0430\u0440\u0430\u043C\u0435\u0442\u0440", selectedOption: key, rgOptions: keys.map(k => ({ data: k, label: labels[k] || k })), onChange: o => setKey(String(o.data)) }), SP_JSX.jsx(DFL.TextField, { label: `Значение: ${data.catalog[key]?.[0] || ''}`, value: value, onChange: e => setValue(e.target.value) }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: add, children: "\u0414\u043E\u0431\u0430\u0432\u0438\u0442\u044C / \u0437\u0430\u043C\u0435\u043D\u0438\u0442\u044C \u0432 \u0447\u0435\u0440\u043D\u043E\u0432\u0438\u043A\u0435" }), Object.entries(draft).map(([k, v]) => SP_JSX.jsxs(DFL.ButtonItem, { disabled: busy, onClick: () => set(k, undefined), children: [labels[k] || k, ": ", v, " \u00B7 \u0443\u0431\u0440\u0430\u0442\u044C"] }, k)), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => preview(draft, scope === 'game')), children: "\u041F\u0440\u0435\u0434\u043F\u0440\u043E\u0441\u043C\u043E\u0442\u0440 \u0431\u0435\u0437 \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u0439" }), scope === 'game' ? SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.ButtonItem, { disabled: busy || !appid, onClick: () => run(async () => { const r = await saveGame(appid, name, draft, resume); unpack(r); if (appid === currentGame().id)
                                    await syncGame(currentGame()); return r; }), children: "\u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435 \u0438\u0433\u0440\u0435" }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy || !appid, onClick: () => run(() => removeGame(appid)), children: "\u0423\u0434\u0430\u043B\u0438\u0442\u044C \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435" })] }) : SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => applyGlobal('decky-global', draft)), children: "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0433\u043B\u043E\u0431\u0430\u043B\u044C\u043D\u043E" }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => applyGlobal('recommended', data.profiles.recommended)), children: "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0440\u0435\u043A\u043E\u043C\u0435\u043D\u0434\u0443\u0435\u043C\u044B\u0439 \u043F\u0440\u043E\u0444\u0438\u043B\u044C" })] })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "\u0421\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435 \u0438 \u043E\u0431\u0441\u043B\u0443\u0436\u0438\u0432\u0430\u043D\u0438\u0435", children: [data.report.managed.map((m, i) => SP_JSX.jsxs(DFL.PanelSectionRow, { children: [labels[m.change.key] || m.change.key, ": ", ['boot', 'zram_config'].includes(m.change.key) ? 'override сохранён; проверка эффекта после reboot' : String(m.actual).slice(0, 120), " ", m.matches ? '✓' : '— расхождение', !m.matches && m.change.kind === 'scalar' && SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => release(m.change.key)), children: "\u041E\u0441\u0442\u0430\u0432\u0438\u0442\u044C \u0444\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u043E\u0435 \u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0435" })] }, i)), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => rollback()), children: "\u041F\u043E\u043B\u043D\u044B\u0439 \u043E\u0442\u043A\u0430\u0442 \u0438 \u043E\u0442\u043A\u043B\u044E\u0447\u0435\u043D\u0438\u0435 \u0430\u0432\u0442\u043E\u043F\u0440\u043E\u0444\u0438\u043B\u0435\u0439" }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => run(() => update()), children: "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C Bosun \u0438 \u043F\u043B\u0430\u0433\u0438\u043D" }), SP_JSX.jsx(DFL.ButtonItem, { disabled: busy, onClick: () => refresh().catch(notifyError), children: "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C \u0444\u0430\u043A\u0442\u0438\u0447\u0435\u0441\u043A\u043E\u0435 \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435" }), message && SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }, children: message }) })] })] });
}
var index = definePlugin(() => {
    const stop = watchGames(SteamClient, currentGame, syncGame, notifyError);
    return { name: 'Bosun', titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "Bosun" }), content: SP_JSX.jsx(Content, {}), icon: SP_JSX.jsx("span", { children: "\u2693" }), onDismount: stop };
});

export { index as default };
