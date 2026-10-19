// SPDX-License-Identifier: GPL-3.0-or-later
export type Game = { id: string; name: string };
type Subscription = { unregister: () => void };
type Client = {
  GameSessions?: { RegisterForAppLifetimeNotifications?: (fn: (n: { unAppID: number; bRunning: boolean }) => void) => Subscription };
  System?: { RegisterForOnResumeFromSuspend?: (fn: () => void) => Subscription };
};

export function watchGames(client: Client, read: () => Game, sync: (game: Game, resume: boolean) => Promise<void>, error: (e: unknown) => void) {
  let queue = Promise.resolve();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let followup: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;
  let active = read();
  let stopped = false;
  const subscriptions: Subscription[] = [];
  const send = (game: Game, resume = false, version = generation) => {
    queue = queue.then(async () => { if (!stopped && version === generation) await sync(game, resume); }).catch(error);
  };
  try {
    const register = client.GameSessions?.RegisterForAppLifetimeNotifications;
    if (register) subscriptions.push(register.call(client.GameSessions, (notification) => {
      if (!notification.bRunning && notification.unAppID && active.id && active.id !== String(notification.unAppID)) return;
      const version = ++generation;
      if (timer) clearTimeout(timer);
      if (followup) clearTimeout(followup);
      const current = read();
      // Lifetime AppID takes precedence over a Router value left from the previous game.
      active = notification.bRunning
        ? notification.unAppID ? { id: String(notification.unAppID), name: current.id === String(notification.unAppID) ? current.name : '' } : current
        : current.id && notification.unAppID && current.id !== String(notification.unAppID) ? current : { id: '', name: '' };
      const target = active;
      timer = setTimeout(() => {
        send(target, false, version);
        // One bounded second pass catches Steam's power reset after game startup.
        // No recurring polling; another lifetime event cancels this pass.
        if (notification.bRunning) followup = setTimeout(() => send(target, false, version), 1500);
      }, 750);
    }));
    const resume = client.System?.RegisterForOnResumeFromSuspend;
    if (resume) subscriptions.push(resume.call(client.System, () => {
      const version = ++generation;
      if (timer) clearTimeout(timer);
      if (followup) clearTimeout(followup);
      timer = setTimeout(() => { active = read(); send(active, true, version); }, 1500);
    }));
  } catch (e) { error(e); }
  send(active);
  if (active.id) followup = setTimeout(() => send(active), 1500);
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    if (followup) clearTimeout(followup);
    subscriptions.forEach(s => s.unregister());
  };
}
