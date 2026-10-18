# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/home/.bosun/app')
from bosun.control import Control
from bosun.core import APP, System


class Plugin:
    async def _main(self):
        self.control = Control(System())
        self.gate = asyncio.Lock()

    async def _call(self, method, *args):
        async with self.gate:
            try:
                data = await asyncio.to_thread(getattr(self.control, method), *args)
                return {'ok': True, 'data': data}
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}

    async def get_state(self):
        return await self._call('state')

    async def save_game(self, appid, name, settings, resume=True):
        return await self._call('save_game', appid, name, settings, resume)

    async def remove_game(self, appid):
        return await self._call('remove_game', appid)

    async def set_automatic(self, enabled):
        return await self._call('automatic', enabled)

    async def activate(self, appid, resume=False):
        return await self._call('activate', appid, resume)

    async def preview(self, settings, game=False):
        return await self._call('preview', settings, game)

    async def apply_global(self, name, settings):
        return await self._call('apply_global', name, settings)

    async def release(self, key):
        return await self._call('release', key)

    async def rollback(self):
        return await self._call('rollback')

    async def measurement_tools(self):
        return await self._call('measurement_tools')

    async def preview_launch(self, account, appid, current, options):
        return await self._call('preview_launch', account, appid, current, options)

    async def prepare_launch(self, account, appid, current, target, mode='write', options=None):
        return await self._call('prepare_launch', account, appid, current, target, mode, options)

    async def finish_launch(self, account, appid, actual):
        return await self._call('finish_launch', account, appid, actual)

    async def update(self):
        # Separate systemd job survives Decky restarting after plugin files are replaced.
        try:
            user = json.loads(Path('/home/.bosun/install.json').read_text())['user']
            result = await asyncio.to_thread(subprocess.run,
                ['systemd-run', '--collect', '--unit=bosun-update', '/usr/bin/python3', APP + '/install.py', '--update', '--user', user],
                capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return {'ok': True, 'data': {'notes': ['Обновление запущено. После перезагрузки Decky откройте Bosun заново. Журнал: journalctl -u bosun-update']}}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    async def _unload(self):
        # Restore only a session owned by this plugin, retain CLI sessions and global profile.
        result = await self._call('activate', '')
        if not result['ok']:
            print('Bosun unload conflict:', result['error'])
