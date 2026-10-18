# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-game launch commands. Never execute Steam options in the privileged backend."""
import re
import shlex


def compose(system, original, options, install=False):
    from .core import BosunError
    from .dependencies import install_package
    if not isinstance(original, str) or len(original) > 8192 or '\x00' in original:
        raise BosunError('Launch options must be a string of at most 8192 characters')
    if not isinstance(options, dict) or set(options) - {'cache_gb', 'dxvk_latency', 'sync', 'radv_wave32', 'radv_nir_cache', 'io_priority', 'affinity'}:
        raise BosunError('Unknown launch setting')
    env, unset, commands, notes = {}, [], [], []
    if 'cache_gb' in options:
        value = options['cache_gb']
        if type(value) is not int or not 1 <= value <= 16:
            raise BosunError('cache_gb must be 1..16')
        env['MESA_SHADER_CACHE_MAX_SIZE'] = str(value) + 'G'
    if 'dxvk_latency' in options:
        value = options['dxvk_latency']
        if type(value) is not int or not 1 <= value <= 4:
            raise BosunError('dxvk_latency must be 1..4')
        env['DXVK_CONFIG'] = f'dxgi.maxFrameLatency = {value}; d3d9.maxFrameLatency = {value}'
    if 'sync' in options:
        value = options['sync']
        if not isinstance(value, str) or value not in {'auto', 'fsync'}:
            raise BosunError('sync must be auto or fsync; esync is obsolete in Proton 11')
        unset.extend(['PROTON_NO_NTSYNC', 'PROTON_NO_FSYNC', 'PROTON_NO_ESYNC'])
        if value != 'auto':
            env['PROTON_NO_NTSYNC'] = '1'
    flags = []
    if 'radv_wave32' in options:
        value = options['radv_wave32']
        choices = {'compute': ['cswave32'], 'pixel': ['pswave32'], 'geometry': ['gewave32'],
                   'all': ['cswave32', 'pswave32', 'gewave32']}
        if not isinstance(value, str) or value not in choices:
            raise BosunError('radv_wave32 must be compute, pixel, geometry or all')
        flags.extend(choices[value])
    if 'radv_nir_cache' in options:
        if type(options['radv_nir_cache']) is not bool:
            raise BosunError('radv_nir_cache must be a boolean')
        if options['radv_nir_cache']:
            flags.append('nircache')
    if flags:
        libraries = [system.path('/usr/lib/libvulkan_radeon.so')]
        compat = system.path('/usr/lib32/libvulkan_radeon.so')
        if compat.exists():
            libraries.append(compat)
        binaries = [library.read_bytes() if library.is_file() and library.stat().st_size < 100 * 1024 * 1024 else b'' for library in libraries]
        supported = [flag for flag in flags if all(b'\0' + flag.encode() + b'\0' in binary for binary in binaries)]
        notes.extend('SKIP RADV_PERFTEST=' + flag + ': installed RADV does not export this option' for flag in flags if flag not in supported)
        if supported:
            env['RADV_PERFTEST'] = ','.join(supported)
    if 'io_priority' in options:
        value = options['io_priority']
        if type(value) is not int or not 0 <= value <= 7:
            raise BosunError('io_priority must be best-effort 0..7; realtime I/O is not used')
        commands.extend(['/usr/bin/ionice', '-c', '2', '-n', str(value)])
    if 'affinity' in options:
        if options['affinity'] != 'physical':
            raise BosunError('affinity must be physical')
        cores = {}
        for path in system.glob('/sys/devices/system/cpu/cpu[0-9]*/topology/core_id'):
            cpu = int(re.search(r'/cpu([0-9]+)/', path)[1])
            parent = path.rsplit('/', 1)[0]
            if system.read(parent.rsplit('/', 1)[0] + '/online', '1') != '1':
                continue
            key = (system.read(parent + '/physical_package_id'), system.read(path))
            cores[key] = min(cpu, cores.get(key, cpu))
        if not cores:
            raise BosunError('CPU topology unavailable; cannot create a physical-core mask')
        commands.extend(['/usr/bin/taskset', '--cpu-list', ','.join(map(str, sorted(cores.values())))])
    try:
        tokens = shlex.split(original)
        lexer = shlex.shlex(original, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        compound = any(t and all(c in ';&|<>()' for c in t) for t in lexer)
    except ValueError as exc:
        raise BosunError('Existing Steam options contain unmatched shell quotes') from exc
    assignments = {t.split('=', 1)[0] for t in tokens if '=' in t}
    overlap = assignments.intersection(set(env) | set(unset))
    if overlap:
        raise BosunError('Existing launch options already set ' + ', '.join(sorted(overlap)) + '; edit them explicitly first')
    first_command = next((token for token in tokens if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*=', token)), '')
    if options and (compound or first_command in {'exec', 'cd', 'export', 'source', '.'}):
        raise BosunError('Existing launch options use shell control syntax; edit the full string in the Steam editor instead of adding a preset')
    for key, executable in [('io_priority', 'ionice'), ('affinity', 'taskset')]:
        if key in options and not system.which(executable):
            notes.append(install_package(system, ['util-linux'], executable) if install else
                         'REQUIRES ' + executable + ': will be installed when launch options are applied')
    prefix = ['/usr/bin/env'] if env or unset else []
    for name in unset:
        prefix.extend(['-u', name])
    prefix.extend(name + '=' + value for name, value in env.items())
    prefix.extend(commands)
    if commands:
        # Existing leading VAR=value assignments still need an env parser after wrappers.
        prefix.append('/usr/bin/env')
    base = original if '%command%' in original else '%command%' + (' ' + original if original else '')
    target = (shlex.join(prefix) + ' ' + base) if prefix else original
    if len(target) > 8192:
        raise BosunError('Generated Steam launch options exceed 8192 characters')
    return {'target': target, 'notes': notes}


def account_key(account, appid):
    from .core import BosunError
    if not isinstance(account, str) or not re.fullmatch('[0-9]{17}', account):
        raise BosunError('Current Steam account unavailable')
    if not isinstance(appid, str) or not re.fullmatch('[1-9][0-9]{0,9}', appid) or int(appid) > 0xffffffff:
        raise BosunError('Steam launch editor requires a 32-bit AppID')
    return account + ':' + appid
