# SPDX-License-Identifier: GPL-3.0-or-later
import re
from .core import BosunError, GLOBAL_KEYS, validate


def apply_global(engine, name, settings, dry=False):
    """An explicit device profile supersedes any overlay, including orphaned CLI sessions."""
    session = engine.state.get('session')
    # Apply directly from the overlay, retaining one transaction and the original journal
    # values. A failure/conflict must keep the session so it can still be recovered.
    notes, conflict = engine.apply(name, validate(settings), dry=dry, supersede_session=True)
    if session:
        source = 'Decky' if session.get('owner') == 'decky' else 'CLI/legacy'
        if dry:
            notes.insert(0, f'При применении временный игровой профиль ({source}) будет заменён глобальным; назначения игр сохранятся.')
        elif not conflict:
            notes.insert(0, f'Временный игровой профиль ({source}) заменён глобальным; назначения игр сохранены.')
    return notes, conflict


def session_action(engine, action, name, token, overlay=None, decky_appid=None):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise BosunError('Invalid session token')
    engine.recover()
    if token in engine.state.get('retired_sessions', []):
        if action == 'session-end':
            return ['Игровой профиль уже заменён глобальным; позднее завершение launcher ничего не меняет.']
        raise BosunError('Session token was superseded; start with a new token')
    if action == 'session-start':
        if engine.state.get('session'):
            raise BosunError('Another game session exists; close it or recover using token from status')
        overlay = validate(overlay)
        current = dict(engine.state['desired'])
        for key in GLOBAL_KEYS:
            if key in overlay and overlay[key] != current.get(key):
                raise BosunError('Game launch cannot change boot, scheduler or SMT. Use runtime-only game profiles.')
        previous = {'profile': engine.state['profile'], 'desired': current}
        engine.state['session'] = {'token': token, 'previous': previous, 'settings': overlay,
                                   'owner': 'decky' if decky_appid else 'cli'}
        if decky_appid:
            engine.state['session']['appid'] = decky_appid
        engine.save()
        settings = {**current, **overlay}
        priority = overlay
    else:
        session = engine.state.get('session')
        if not session or session['token'] != token:
            raise BosunError('Session token mismatch (see status)')
        settings, name = session['previous']['desired'], session['previous']['profile']
        priority = session.get('settings', {k: v for k, v in engine.state['desired'].items() if settings.get(k) != v})
    try:
        notes, conflict = engine.apply(name, settings, priority_keys=priority, supersede_session=action == 'session-end')
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
