# SPDX-License-Identifier: GPL-3.0-or-later
import re
from .core import BosunError, GLOBAL_KEYS, validate


def apply_global(engine, name, settings, dry=False):
    """An explicit device profile supersedes a Decky overlay, even after a lost exit event."""
    session = engine.state.get('session')
    stale = engine.state['boot_id'] != engine.sys.read('/proc/sys/kernel/random/boot_id', '')
    if session and not stale and session.get('owner') != 'decky':
        raise BosunError('CLI launcher owns the active profile; close it or end its session using the token from status')
    # Apply directly from the overlay, retaining one transaction and the original journal
    # values. A failure/conflict must keep the session so it can still be recovered.
    notes, conflict = engine.apply(name, validate(settings), dry=dry)
    if session and session.get('owner') == 'decky':
        if dry:
            notes.insert(0, 'При применении временный профиль Decky будет заменён глобальным; назначение игре сохранится.')
        elif not conflict:
            notes.insert(0, 'Временный профиль Decky заменён глобальным; назначение игре сохранено.')
    if session and not dry and not conflict:
        engine.state.pop('session', None)
        engine.save()
    return notes, conflict


def session_action(engine, action, name, token, overlay=None):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise BosunError('Invalid session token')
    engine.recover()
    if action == 'session-start':
        if engine.state.get('session'):
            raise BosunError('Another game session exists; close it or recover using token from status')
        overlay = validate(overlay)
        current = dict(engine.state['desired'])
        for key in GLOBAL_KEYS:
            if key in overlay and overlay[key] != current.get(key):
                raise BosunError('Game launch cannot change boot, scheduler or SMT. Use runtime-only game profiles.')
        previous = {'profile': engine.state['profile'], 'desired': current}
        engine.state['session'] = {'token': token, 'previous': previous}
        engine.save()
        settings = {**current, **overlay}
    else:
        session = engine.state.get('session')
        if not session or session['token'] != token:
            raise BosunError('Session token mismatch (see status)')
        settings, name = session['previous']['desired'], session['previous']['profile']
    try:
        notes, conflict = engine.apply(name, settings)
    except BaseException:
        if action == 'session-start' and not engine.state.get('pending'):
            engine.state.pop('session', None)
            engine.save()
        raise
    if conflict:
        raise BosunError('Session conflict; settings retained for explicit resolution: ' + '; '.join(notes))
    if action == 'session-end':
        engine.state.pop('session', None)
        engine.save()
    return notes
