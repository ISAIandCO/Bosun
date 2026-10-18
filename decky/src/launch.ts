// SPDX-License-Identifier: GPL-3.0-or-later
type Details = { strLaunchOptions?: string; strShortcutLaunchOptions?: string; strShortcutExe?: string };
type Apps = {
  RegisterForAppDetails: (id: number, callback: (details: Details) => void) => { unregister: () => void };
  SetAppLaunchOptions: (id: number, text: string) => void;
  SetShortcutLaunchOptions: (id: number, text: string) => void;
};
export type LaunchState = { text: string; shortcut: boolean };
export function readLaunch(apps: Apps, id: number): Promise<LaunchState> {
  return new Promise((resolve, reject) => {
    let subscription: { unregister: () => void } | undefined;
    let done = false;
    const timeout = setTimeout(() => { done = true; subscription?.unregister(); reject(new Error('Steam AppDetails timeout')); }, 5000);
    try {
      subscription = apps.RegisterForAppDetails(id, details => {
        if (done) return;
        const shortcut = !!details.strShortcutExe;
        const text = shortcut ? details.strShortcutLaunchOptions : details.strLaunchOptions;
        if (typeof text !== 'string') return;
        done = true; clearTimeout(timeout);
        resolve({ text, shortcut });
        subscription?.unregister();
      });
      if (done) subscription.unregister();
    } catch (error) { done = true; clearTimeout(timeout); subscription?.unregister(); reject(error); }
  });
}
export async function writeLaunch(apps: Apps, id: number, expected: string, target: string, sameAccount = () => true): Promise<string> {
  const current = await readLaunch(apps, id);
  if (!sameAccount()) throw new Error('Steam account changed; refusing launch-option write');
  if (current.text !== expected) throw new Error('Steam launch options changed; refusing overwrite');
  if (current.shortcut) apps.SetShortcutLaunchOptions(id, target);
  else apps.SetAppLaunchOptions(id, target);
  for (let attempt = 0; attempt < 8; attempt++) {
    if (!sameAccount()) throw new Error('Steam account changed; backup retained');
    const readback = await readLaunch(apps, id);
    if (readback.text === target) return target;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Steam did not confirm launch options; original backup retained');
}
