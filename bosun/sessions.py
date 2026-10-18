# SPDX-License-Identifier: GPL-3.0-or-later
import re
from .core import BosunError, GLOBAL_KEYS, validate


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
