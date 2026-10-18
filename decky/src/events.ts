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
  let stopped = false;
  const subscriptions: Subscription[] = [];
  const send = (game: Game, resume = false) => {
    queue = queue.then(async () => { if (!stopped) await sync(game, resume); }).catch(error);
  };
  try {
    const register = client.GameSessions?.RegisterForAppLifetimeNotifications;
    if (register) subscriptions.push(register.call(client.GameSessions, (notification) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        const current = read();
        // Steam reports ID 0 for some non-Steam shortcuts; Router supplies their actual ID.
        if (!notification.bRunning && (!notification.unAppID || current.id === String(notification.unAppID))) send({ id: '', name: '' });
        else send(current.id ? current : notification.bRunning && notification.unAppID ? { id: String(notification.unAppID), name: '' } : current);
      }, 750);
    }));
    const resume = client.System?.RegisterForOnResumeFromSuspend;
    if (resume) subscriptions.push(resume.call(client.System, () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => send(read(), true), 1500);
    }));
  } catch (e) { error(e); }
  send(read());
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    subscriptions.forEach(s => s.unregister());
  };
}
