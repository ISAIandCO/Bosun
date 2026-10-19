# SPDX-License-Identifier: GPL-3.0-or-later
"""Capability-gated SteamOS tuning with journalled changes and no polling or shell input."""
import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from . import tweaks

VERSION = '0.4.2'
APP = '/home/.bosun/app'
STATE = '/home/.bosun/state'
BOOT = '/etc/default/grub.d/zz-bosun.cfg'
ZRAM = '/etc/systemd/zram-generator.conf.d/90-bosun.conf'
POWER = {'governor', 'epp', 'boost', 'smt', 'cpu_max_mhz', 'tdp_w', 'gpu_mode'}
BOOT_KEYS = {'mitigations', 'moverate', 'lbpw', 'workqueue_power_efficient'} | tweaks.EXTRA_BOOT
GLOBAL_KEYS = BOOT_KEYS | tweaks.GLOBAL_EXTRA | {'zram_algorithm', 'zram_size_percent', 'lavd', 'smt'}
RECOMMENDED = {
    'mitigations': 'off', 'nmi_watchdog': 0, 'zram_page_cluster': 0,
    'mglru': 'full', 'thp_defrag': 'defer+madvise', 'home_noatime': 1, 'zswap': 'off',
}
PROFILES = {
    'recommended': RECOMMENDED,
    'cpu-game': {'governor': 'powersave', 'epp': 'balance_performance'},
    'gpu-game': {'governor': 'powersave', 'epp': 'balance_power', 'cpu_max_mhz': 2800},
    'battery': {'governor': 'powersave', 'epp': 'balance_power', 'boost': 0},
    'memory-pressure': {'swappiness': 150, 'zram_page_cluster': 0, 'mglru': 'on'},
    'memory-throughput': {'mglru': 'full', 'thp': 'always', 'thp_shmem': 'within_size',
                          'compaction_proactiveness': 0, 'page_lock_unfairness': 8},
    'streaming': {'dirty_background_mib': 200, 'dirty_mib': 400, 'vfs_cache_pressure': 50},
    'io-throughput': {'nvme_wbt_lat_usec': 0, 'sd_wbt_lat_usec': 0, 'sd_bfq_slice_idle_us': 0},
    'background-overhead': {'nmi_watchdog': 0, 'soft_watchdog': 0, 'hung_task_timeout_secs': 0,
                            'vm_stat_interval': 15, 'ksm': 0},
    'stock': {},
}
# Values are candidates for A/B testing, never measured Deck performance claims.
CATALOG = {
    'mitigations': ('off', 'Boot: disable software CPU vulnerability mitigations; reboot required.'),
    'nmi_watchdog': ('0|1', 'Hard-lockup detector perf interrupt; 0 removes it, reduces hang diagnostics.'),
    'zram_page_cluster': ('0..3', 'Swap readahead exponent; only when highest-priority swap is zram.'),
    'mglru': ('on|full|off', 'on changes main bit; full enables all three documented components and preserves other bits.'),
    'thp_defrag': ('always|defer|defer+madvise|madvise|never', 'Allocation compaction policy; stalls vs huge-page coverage.'),
    'thp': ('always|madvise|never', 'Experimental huge-page allocation policy.'),
    'swappiness': ('0..200', 'Experimental relative reclaim cost; >100 requires active zram here.'),
    'governor': ('available governor', 'Per-game CPU frequency policy; driver-specific.'),
    'epp': ('available EPP name', 'Per-game CPPC energy hint; may reduce CPU-bound FPS/lows.'),
    'boost': ('0|1', 'Per-game CPU boost; disabling can free APU budget, can hurt CPU-bound games.'),
    'smt': ('on|off', 'Experimental CPU topology; can hurt threaded games. Never forceoff.'),
    'cpu_max_mhz': ('400..3500', 'CPUFreq policy ceiling; hardware bounds checked, min lowered only if required. Verify actual clocks under load; not a guaranteed SMU/Boost hard cap.'),
    'tdp_w': ('4..15', 'Per-game APU cap in watts, NOT total battery power.'),
    'gpu_mode': ('auto|low|high', 'AMDGPU dynamic clock policy; low/high may badly redistribute budget.'),
    'nvme_scheduler': ('available scheduler', 'Experimental storage latency/throughput tradeoff.'),
    'sd_scheduler': ('available scheduler', 'Experimental microSD scheduler; detected by device type.'),
    'moverate': ('8..1024', 'Experimental boot AMDGPU migration rate MB/s; version-dependent.'),
    'lbpw': ('-1|0|1', 'Legacy compatibility key; skipped on Deck RDNA2 because upstream LBPW is GFX9-specific.'),
    'compaction_proactiveness': ('0..100', 'Experimental background memory compaction; 0 removes proactive work but may increase fragmentation.'),
    'nvme_readahead_kb': ('0..4096', 'Experimental NVMe read-ahead; loading/streaming vs unnecessary reads.'),
    'sd_readahead_kb': ('0..4096', 'Experimental SD read-ahead; storage workload-specific.'),
    'zram_algorithm': ('lz4|lzo-rle|zstd', 'Reboot-only compressor override; never live swapoff.'),
    'zram_size_percent': ('25..200', 'Reboot-only virtual zram capacity as percent of RAM, not RAM reservation.'),
    'workqueue_power_efficient': ('0|1', 'Experimental boot WQ_POWER_EFFICIENT consolidation; may trade latency for fewer wakeups.'),
    'lavd': ('autopilot|balanced|powersave|performance', 'Experimental installed scx_lavd mode; kernel/BTF required, no other scx owner.'),
}
CATALOG.update(tweaks.CATALOG)


class BosunError(RuntimeError):
    pass


def atomic(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.bosun-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def selected(text):
    match = re.search(r'\[([^]]+)\]', text)
    return match[1] if match else text.strip()


class System:
    def __init__(self, root='/'):
        self.root = Path(root).resolve()
        self.fake = self.root != Path('/')
        if self.fake and not (self.root / '.bosun-test-root').exists():
            raise BosunError('Test root requires .bosun-test-root marker')
        self.commands = []

    def path(self, path):
        candidate = self.root / str(path).lstrip('/')
        if self.fake and not candidate.resolve().is_relative_to(self.root):
            raise BosunError('Path escapes test root')
        return candidate

    def read(self, path, default=None):
        try:
            return self.path(path).read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return default

    def glob(self, pattern):
        return sorted('/' + str(p.relative_to(self.root)) for p in self.root.glob(pattern.lstrip('/')))

    def command(self, *args, check=True, timeout=45):
        self.commands.append(list(args))
        if self.fake:
            return ''
        env = {**os.environ, 'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LD_LIBRARY_PATH': ''}
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        if check and p.returncode:
            raise BosunError(f'{args[0]}: {p.stderr.strip() or p.stdout.strip()}')
        return p.stdout.strip()

    def which(self, name):
        if self.fake:
            return next((p for p in ['/usr/bin/' + name, '/usr/sbin/' + name] if self.path(p).exists()), None)
        return shutil.which(name, path='/usr/sbin:/usr/bin:/sbin:/bin')

    def restore_readonly(self):
        lease = self.path(STATE + '/readonly-lease.json')
        if lease.exists():
            data = json.loads(lease.read_text())
            if data.get('original') != 'enabled':
                raise BosunError('Invalid read-only recovery lease')
            self.command('steamos-readonly', 'enable')
            lease.unlink()

    @contextlib.contextmanager
    def writable(self):
        # /home and SteamOS /etc are normally writable. Boot/install edits enter here.
        enabled = False
        lease = self.path(STATE + '/readonly-lease.json')
        if self.which('steamos-readonly'):
            status = self.command('steamos-readonly', 'status', check=False).lower().split(',')[0]
            if not self.fake and status not in {'enabled', 'disabled'}:
                raise BosunError(f'Unknown steamos-readonly status: {status}')
            enabled = status == 'enabled' or lease.exists()
        if enabled:
            atomic(lease, json.dumps({'original': 'enabled'}))
        try:
            if enabled:
                self.command('steamos-readonly', 'disable')
            yield
        finally:
            if enabled:
                self.command('steamos-readonly', 'enable')
                lease.unlink(missing_ok=True)

    def fingerprint(self):
        # No fragile numeric version allowlist; a change pauses automatic replay.
        bits = [self.read('/etc/os-release', ''), self.read('/proc/sys/kernel/osrelease', '')]
        if self.which('pacman'):
            bits.append(self.command('pacman', '-Q', 'mesa', 'vulkan-radeon', 'gamescope', check=False))
        return hashlib.sha256('\n'.join(bits).encode()).hexdigest()

    def info(self):
        osinfo = {}
        for line in self.read('/etc/os-release', '').splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                osinfo[k] = v.strip('"')
        board = self.read('/sys/class/dmi/id/board_name', '')
        product = self.read('/sys/class/dmi/id/product_name', '')
        model = {'Jupiter': 'LCD', 'Galileo': 'OLED'}.get(board) or {'Jupiter': 'LCD', 'Galileo': 'OLED'}.get(product)
        policies = self.glob('/sys/devices/system/cpu/cpufreq/policy[0-9]*')
        versions = {}
        if self.which('pacman'):
            versions['packages'] = self.command('pacman', '-Q', 'mesa', 'vulkan-radeon', 'gamescope', check=False)
        return {'bosun': VERSION, 'model': model, 'board': board, 'product': product,
                'os': osinfo, 'kernel': self.read('/proc/sys/kernel/osrelease', ''),
                'bios': self.read('/sys/class/dmi/id/bios_version'), 'versions': versions,
                'fingerprint': self.fingerprint(), 'boot_id': self.read('/proc/sys/kernel/random/boot_id', ''),
                'cpu': {p: {k: self.read(p + '/' + k) for k in ['scaling_driver', 'scaling_governor',
                         'energy_performance_preference', 'scaling_min_freq', 'scaling_max_freq',
                         'cpuinfo_min_freq', 'cpuinfo_max_freq', 'cpuinfo_cur_freq', 'scaling_cur_freq',
                         'scaling_available_governors', 'boost']} for p in policies},
                'cmdline': self.read('/proc/cmdline'), 'swap': self.read('/proc/swaps'),
                'scx': self.read('/sys/kernel/sched_ext/state'),
                'scx_ops': self.read('/sys/kernel/sched_ext/root/ops'),
                'amdgpu': {k: self.read('/sys/module/amdgpu/parameters/' + k) for k in ['moverate', 'lbpw']},
                'extra_runtime': {k: self.read(spec[0]) for k, spec in tweaks.SCALARS.items()},
                'home_mount': tweaks.mount_info(self),
                'cpu_mode': self.read('/sys/devices/system/cpu/amd_pstate/status'),
                'gpu_sched_policy': self.read('/sys/module/gpu_sched/parameters/sched_policy'),
                'epp_boost': self.read('/sys/module/amd_pstate/parameters/epp_boost'),
                'runtime': {p: self.read(p) for p in [
                    '/proc/sys/kernel/nmi_watchdog', '/proc/sys/vm/page-cluster', '/proc/sys/vm/swappiness',
                    '/proc/sys/vm/compaction_proactiveness', '/sys/module/workqueue/parameters/power_efficient',
                    '/sys/kernel/mm/lru_gen/enabled', '/sys/kernel/mm/transparent_hugepage/enabled',
                    '/sys/kernel/mm/transparent_hugepage/defrag', '/sys/devices/system/cpu/smt/control',
                    '/sys/devices/system/cpu/cpufreq/boost']},
                'gpu': {p: self.read(p) for pattern in [
                    '/sys/class/drm/card[0-9]*/device/power_dpm_force_performance_level',
                    '/sys/class/drm/card[0-9]*/device/pp_dpm_sclk',
                    '/sys/class/drm/card[0-9]*/device/uevent'] for p in self.glob(pattern)},
                'storage': {p: self.read(p) for pattern in ['/sys/block/*/queue/scheduler', '/sys/block/*/queue/read_ahead_kb'] for p in self.glob(pattern)},
                'zram': {p: self.read(p) for pattern in ['/sys/block/zram*/comp_algorithm', '/sys/block/zram*/disksize', '/sys/block/zram*/mm_stat'] for p in self.glob(pattern)},
                'power_caps_uw': {p: self.read(p) for p in self.glob('/sys/class/hwmon/hwmon*/power[12]_cap')},
                'model_specs': {'cpu': 'Zen 2 4c/8t', 'gpu': '8 RDNA2 CUs, up to 1600 MHz',
                    'apu_nm': 6 if model == 'OLED' else 7 if model == 'LCD' else None,
                    'ram_gb': 16, 'ram_mt_s': 6400 if model == 'OLED' else 5500 if model == 'LCD' else None,
                    'battery_design_wh': 50 if model == 'OLED' else 40 if model == 'LCD' else None,
                    'panel_max_hz': 90 if model == 'OLED' else 60 if model == 'LCD' else None, 'apu_range_w': [4, 15]},
                'cpu_vulnerabilities': {p: self.read(p) for p in self.glob('/sys/devices/system/cpu/vulnerabilities/*')},
                'readonly': self.command('steamos-readonly', 'status', check=False) if self.which('steamos-readonly') else 'unavailable'}

    def check_device(self):
        info = self.info()
        if not info['model'] or info['os'].get('ID') != 'steamos':
            raise BosunError('System changes require SteamOS on Steam Deck Jupiter (LCD) or Galileo (OLED). Report still works.')

    def power_conflicts(self):
        units = ['power-profiles-daemon.service', 'tlp.service', 'auto-cpufreq.service',
                 'tuned.service', 'sdweak.service']
        found = [u for u in units if self.command('systemctl', 'is-active', u, check=False) == 'active']
        for path in self.glob('/home/*/homebrew/plugins/*/plugin.json'):
            try:
                name = json.loads(self.read(path, '{}')).get('name', '').lower()
                if name in {'powertools', 'simpledeckytdp'}:
                    found.append(path)
            except ValueError:
                continue
        return found

    def boot_target(self):
        # Use the SteamOS wrapper's configured EFI destination, never guess a PC GRUB path.
        wrapper = self.read('/usr/bin/update-grub', '')
        match = re.search(r'^\s*file=((?:/boot)?/efi/[A-Za-z0-9_./-]+)\s*$', wrapper, re.M)
        if not match or '..' in Path(match[1]).parts:
            raise BosunError('Unsupported update-grub layout: cannot identify SteamOS EFI grub.cfg target')
        target = match[1]
        if not self.path(target).is_file():
            raise BosunError(f'Boot config missing: {target}')
        source = self.read('/usr/bin/grub-mkconfig', '') or self.read('/usr/sbin/grub-mkconfig', '')
        if '/etc/default/grub.d/' not in source and '${sysconfdir}/default/grub.d/' not in source:
            raise BosunError('grub-mkconfig does not advertise grub.d overrides')
        if not self.which('grub-script-check'):
            raise BosunError('grub-script-check not installed')
        return target

    def regenerate_boot(self, expected):
        target = self.boot_target()
        before = self.path(target).read_bytes()
        if self.fake:
            self.command('grub-mkconfig', '-o', 'isolated-output')
            self.command('grub-script-check', 'isolated-output')
            return
        with tempfile.TemporaryDirectory(prefix='bosun-grub-') as d:
            new = Path(d) / 'grub.cfg'
            self.command('grub-mkconfig', '-o', str(new), timeout=120)
            self.command('grub-script-check', str(new))
            text = new.read_text()
            # SteamOS prefixes the kernel loader with its steamenv_boot command.
            linux = [line for line in text.splitlines() if re.match(r'\s*(?:steamenv_boot\s+)?linux(?:efi)?\s', line)]
            if not linux:
                raise BosunError('Generated GRUB has no Linux entries')
            for arg in expected:
                if not any(arg in line.split() for line in linux):
                    raise BosunError(f'Generated GRUB missing {arg}; refusing boot update')
            if self.path(target).read_bytes() != before:
                raise BosunError('GRUB changed during generation; refusing overwrite')
            # Retain an exact recovery copy of the latest pre-edit config (not used on normal rollback).
            atomic(self.path(STATE + '/grub-before.cfg'), before.decode(), 0o600)
            atomic(self.path(target), text, self.path(target).stat().st_mode & 0o777)


@dataclasses.dataclass
class Change:
    key: str
    path: str
    value: str
    kind: str = 'scalar'
    choices: tuple = ()

    @property
    def id(self):
        return self.kind + ':' + self.path

    def current(self, system):
        if self.kind == 'file':
            p = system.path(self.path)
            return p.read_text() if p.exists() else None
        if self.kind == 'service':
            return system.command('systemctl', 'is-active', self.path, check=False) or 'inactive'
        if self.key == 'home_noatime':
            info = tweaks.mount_info(system)
            return tweaks.atime_flags(info) if info else None
        if self.key in {'dirty_background_mib', 'dirty_mib'}:
            return json.dumps([int(system.read(self.path, '0')), int(system.read(self.path.replace('_bytes', '_ratio'), '0'))])
        raw = selected(system.read(self.path, ''))
        if self.key == 'zswap':
            return 'Y' if raw.lower() in {'1', 'y', 'yes', 'true'} else 'N'
        if self.key == 'swap_vma_readahead':
            return 'true' if raw.lower() in {'1', 'y', 'yes', 'true'} else 'false'
        return hex(int(raw, 0)) if self.key == 'mglru' and raw else raw

    def identity(self, system):
        if self.kind != 'scalar':
            return self.path
        if self.key == 'home_noatime':
            info = tweaks.mount_info(system)
            return ':'.join(str(info.get(k)) for k in ['target', 'source', 'fstype']) if info else 'unmounted'
        resolved = str(system.path(self.path).resolve())
        resolved = re.sub(r'/hwmon/hwmon[0-9]+/', '/hwmon/', resolved)
        if '/sys/block/mmcblk' in self.path:
            device = self.path.split('/queue/')[0]
            resolved += ':' + system.read(device + '/device/cid', '')
        if '/sys/class/hwmon/' in self.path:
            resolved += ':' + system.read(str(Path(self.path).parent) + '/name', '')
        return resolved

    def put(self, system, value):
        if self.kind == 'file':
            if value is None:
                system.path(self.path).unlink(missing_ok=True)
            else:
                atomic(system.path(self.path), value, 0o644)
        elif self.kind == 'service':
            system.command('systemctl', 'daemon-reload')
            action = 'restart' if value == 'active' and self.current(system) == 'active' else 'start' if value == 'active' else 'stop'
            system.command('systemctl', action, self.path)
        elif self.key == 'home_noatime':
            if not re.fullmatch(r'(noatime|relatime|strictatime),(no)?diratime', value):
                raise BosunError('Invalid saved mount policy')
            info = tweaks.mount_info(system)
            if info is None:
                raise BosunError('/home mount disappeared')
            kept = [flag for flag in info['vfs-options'].split(',') if flag not in {'atime', 'noatime', 'relatime', 'strictatime', 'nodiratime', 'diratime'}]
            # Both source and target avoid merging stale /etc/fstab flags into the remount.
            system.command('mount', '-o', ','.join(['remount', *kept, value]), info['source'], '/home')
        elif self.key in {'dirty_background_mib', 'dirty_mib'}:
            byte_count, ratio = json.loads(value)
            path, target = (self.path, byte_count) if byte_count else (self.path.replace('_bytes', '_ratio'), ratio)
            with system.path(path).open('w') as f:
                f.write(str(target) + '\n')
            if system.fake:
                system.path(self.path.replace('_bytes', '_ratio') if byte_count else self.path).write_text('0\n')
        else:
            with system.path(self.path).open('w') as f:
                f.write(str(value) + '\n')
        actual = self.current(system)
        if not system.fake and actual != value:
            raise BosunError(f'{self.path}: requested {value!r}, read back {actual!r}')


def validate(settings):
    if not isinstance(settings, dict):
        raise BosunError('Profile settings must be an object')
    bounds = {'nmi_watchdog': (0, 1), 'zram_page_cluster': (0, 3), 'swappiness': (0, 200),
              'boost': (0, 1), 'cpu_max_mhz': (400, 3500), 'tdp_w': (4, 15),
              'moverate': (8, 1024), 'lbpw': (-1, 1), 'compaction_proactiveness': (0, 100),
              'nvme_readahead_kb': (0, 4096), 'sd_readahead_kb': (0, 4096),
              'zram_size_percent': (25, 200), 'workqueue_power_efficient': (0, 1)}
    enums = {'mitigations': ['off'], 'mglru': ['on', 'off'], 'smt': ['on', 'off'],
             'gpu_mode': ['auto', 'low', 'high'], 'lavd': ['autopilot', 'balanced', 'powersave', 'performance'],
             'thp': ['always', 'madvise', 'never'], 'zram_algorithm': ['lz4', 'lzo-rle', 'zstd'],
             'thp_defrag': ['always', 'defer', 'defer+madvise', 'madvise', 'never']}
    bounds.update(tweaks.BOUNDS)
    enums.update(tweaks.ENUMS)
    enums['mglru'].append('full')
    for key in settings:
        for prefix, scheduler in [('nvme_kyber_', 'kyber'), ('sd_bfq_', 'bfq')]:
            device = prefix.split('_')[0]
            if key.startswith(prefix) and device + '_scheduler' in settings and settings[device + '_scheduler'] != scheduler:
                raise BosunError(f'{key} requires {device}_scheduler={scheduler}')
    pair = {'dirty_background_mib', 'dirty_mib'}
    if pair.intersection(settings) and (not pair.issubset(settings) or
            type(settings['dirty_background_mib']) is not int or type(settings['dirty_mib']) is not int or
            settings['dirty_background_mib'] >= settings['dirty_mib']):
        raise BosunError('Set dirty_background_mib and dirty_mib together; background must be lower')
    for key, value in settings.items():
        if key not in CATALOG:
            raise BosunError('Unknown setting: ' + key)
        if key in bounds:
            lo, hi = bounds[key]
            if type(value) is not int or not lo <= value <= hi:
                raise BosunError(f'{key}: expected integer {lo}..{hi}')
        elif key in enums:
            if value not in enums[key]:
                raise BosunError(f'{key}: expected {enums[key]}')
        elif not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,40}', value):
            raise BosunError(f'Invalid value for {key}')
    return settings


def boot_content(args):
    lines = ['# Managed by Bosun. Remove through bosun rollback.\n']
    # Strip only keys owned by this override from the current vendor/third-party string.
    for arg in args:
        key = arg.split('=')[0]
        lines.append('GRUB_CMDLINE_LINUX_DEFAULT="$(printf \'%s\\n\' "$GRUB_CMDLINE_LINUX_DEFAULT" '
                     f"| sed -E 's/(^| ){re.escape(key)}=[^ ]*//g') {arg}\"\n")
    return ''.join(lines)


def plan(system, settings, managed=()):
    validate(settings)
    changes, skipped = tweaks.extra_plan(system, settings)
    policies = system.glob('/sys/devices/system/cpu/cpufreq/policy[0-9]*')
    conflicts = system.power_conflicts() if POWER.intersection(settings) else []
    swaps = [line.split() for line in system.read('/proc/swaps', '').splitlines()[1:] if line.split()]
    highest = max((int(row[4]) for row in swaps), default=None)
    zram = bool(swaps) and all(row[0].startswith('/dev/zram') for row in swaps if int(row[4]) == highest)
    bootargs = []

    def add(key, path, value, choices=()):
        if not system.path(path).exists():
            skipped.append(f'{key}: interface missing: {path}')
        elif choices and str(value) not in choices:
            skipped.append(f'{key}: {value} unavailable; available: {choices}')
        else:
            changes.append(Change(key, path, str(value), choices=tuple(choices)))

    for key, value in settings.items():
        if key in tweaks.CATALOG and key not in tweaks.EXTRA_BOOT:
            continue
        if key in POWER and conflicts:
            skipped.append(f'{key}: another power manager detected: {", ".join(conflicts)}; disable it for Bosun power profiles')
            continue
        if key in BOOT_KEYS:
            if key in tweaks.EXTRA_BOOT:
                args = tweaks.boot_arg(system, key, value)
                if args:
                    bootargs.extend(args)
                else:
                    skipped.append(f'SKIP {key}: required kernel/CPPC interface unavailable')
                continue
            if key == 'lbpw':
                skipped.append('lbpw: upstream implementation is limited to GFX9 IPs; Steam Deck LCD/OLED use RDNA2, so this tweak is not applied')
                continue
            if key == 'workqueue_power_efficient':
                if system.path('/sys/module/workqueue/parameters/power_efficient').exists():
                    bootargs.append('workqueue.power_efficient=' + str(value))
                else:
                    skipped.append('workqueue_power_efficient: kernel interface unavailable')
                continue
            if key != 'mitigations' and system.read('/sys/module/amdgpu/parameters/' + key) is None:
                skipped.append(f'{key}: amdgpu parameter not exported by this kernel')
                continue
            bootargs.append(('amdgpu.' if key != 'mitigations' else '') + key + '=' + str(value))
        elif key == 'nmi_watchdog':
            add(key, '/proc/sys/kernel/nmi_watchdog', value)
        elif key in {'swappiness', 'zram_page_cluster'}:
            if (key == 'zram_page_cluster' or value > 100) and not zram:
                skipped.append(f'{key}: highest-priority active swap is not exclusively zram')
            else:
                add(key, '/proc/sys/vm/' + ('page-cluster' if key == 'zram_page_cluster' else key), value)
        elif key == 'compaction_proactiveness':
            add(key, '/proc/sys/vm/compaction_proactiveness', value)
        elif key in {'zram_algorithm', 'zram_size_percent'}:
            continue  # one consolidated reboot-only generator override below
        elif key == 'mglru':
            path = '/sys/kernel/mm/lru_gen/enabled'
            raw = system.read(path)
            if raw is None:
                skipped.append('mglru: kernel lacks CONFIG_LRU_GEN')
            else:
                current = int(raw, 0)
                add(key, path, hex((current | (7 if value == 'full' else 1)) if value != 'off' else (current & ~1)))
        elif key in {'thp', 'thp_defrag'}:
            path = '/sys/kernel/mm/transparent_hugepage/' + ('enabled' if key == 'thp' else 'defrag')
            choices = re.sub(r'[\[\]]', '', system.read(path, '')).split()
            add(key, path, value, choices)
        elif key in {'governor', 'epp', 'cpu_max_mhz', 'boost'}:
            if key == 'boost' and system.path('/sys/devices/system/cpu/cpufreq/boost').exists():
                add(key, '/sys/devices/system/cpu/cpufreq/boost', value)
                continue
            if not policies:
                skipped.append(f'{key}: no cpufreq policy')
            for p in policies:
                if key == 'governor':
                    if value == 'powersave' and 'epp' in settings and system.read(p + '/scaling_driver') not in {'amd-pstate-epp', 'amd_pstate_epp'}:
                        skipped.append(f'governor: {p} EPP profile requires active amd-pstate-epp; legacy powersave would force low clocks')
                        continue
                    add(key, p + '/scaling_governor', value, system.read(p + '/scaling_available_governors', '').split())
                elif key == 'epp':
                    driver = system.read(p + '/scaling_driver', '')
                    gov = settings.get('governor', system.read(p + '/scaling_governor'))
                    if driver not in {'amd-pstate-epp', 'amd_pstate_epp'} or gov != 'powersave':
                        skipped.append(f'epp: {p} requires active amd-pstate-epp with powersave governor')
                    else:
                        add(key, p + '/energy_performance_preference', value,
                            system.read(p + '/energy_performance_available_preferences', '').split())
                elif key == 'cpu_max_mhz':
                    hz = value * 1000
                    minimum = int(system.read(p + '/scaling_min_freq', '0'))
                    hardware_min = int(system.read(p + '/cpuinfo_min_freq', str(minimum)))
                    maximum = int(system.read(p + '/cpuinfo_max_freq', '0'))
                    if hardware_min <= hz <= maximum:
                        old = managed.get('scalar:' + p + '/scaling_min_freq') if isinstance(managed, dict) else None
                        floor = int(old['original']) if old else minimum
                        if minimum > hz or old:
                            add(key, p + '/scaling_min_freq', min(floor, hz))
                        add(key, p + '/scaling_max_freq', hz)
                    else:
                        skipped.append(f'{key}: {p} requested ceiling outside hardware range [{hardware_min}, {maximum}] kHz')
                else:
                    add(key, p + '/boost', value)
        elif key == 'smt':
            path = '/sys/devices/system/cpu/smt/control'
            if system.read(path) not in {'on', 'off'}:
                skipped.append('smt: unsupported or permanently forceoff')
            else:
                add(key, path, value)
        elif key == 'tdp_w':
            caps = []
            for base in system.glob('/sys/class/hwmon/hwmon*'):
                if system.read(base + '/name') not in {'amdgpu', 'steamdeck_hwmon', 'steamdeck'}:
                    continue
                for path in system.glob(base + '/power[12]_cap'):
                    lower = int(system.read(path + '_min', '4000000'))
                    upper = min(15000000, int(system.read(path + '_max', '15000000')))
                    if lower <= value * 1000000 <= upper:
                        caps.append(path)
                        add(key, path, value * 1000000)
                    else:
                        skipped.append(f'tdp_w: {path} outside detected cap range')
            if not caps:
                skipped.append('tdp_w: no compatible Steam Deck hwmon power caps')
        elif key == 'gpu_mode':
            cards = [p for p in system.glob('/sys/class/drm/card[0-9]*/device')
                     if system.read(p + '/vendor') == '0x1002']
            if len(cards) != 1:
                skipped.append('gpu_mode: requires exactly one AMD GPU; disconnect eGPU')
            else:
                add(key, cards[0] + '/power_dpm_force_performance_level', value)
        elif key in {'nvme_scheduler', 'sd_scheduler', 'nvme_readahead_kb', 'sd_readahead_kb'}:
            paths = system.glob('/sys/block/' + ('nvme*n*' if key.startswith('nvme_') else 'mmcblk[0-9]') + '/queue/' + ('read_ahead_kb' if key.endswith('readahead_kb') else 'scheduler'))
            if not paths:
                skipped.append(f'{key}: no matching storage device')
            for path in paths:
                if key.startswith('sd_') and system.read(path.split('/queue/')[0] + '/device/type') != 'SD':
                    skipped.append(f'{key}: not an SD card: {path}')
                    continue
                choices = () if key.endswith('readahead_kb') else re.sub(r'[\[\]]', '', system.read(path, '')).split()
                add(key, path, value, choices)
        elif key == 'lavd':
            binpath = system.which('scx_lavd')
            own = 'service:bosun-lavd.service' in managed
            if not binpath or not system.path('/sys/kernel/sched_ext/state').exists() or not system.path('/sys/kernel/btf/vmlinux').exists():
                skipped.append('lavd: requires installed scx_lavd, kernel sched_ext and BTF; stock scheduler retained')
            elif system.path('/etc/systemd/system/bosun-lavd.service').exists() and not own:
                skipped.append('lavd: existing bosun-lavd.service is not owned by Bosun')
            elif system.read('/sys/kernel/sched_ext/state') == 'enabled' and not (own and
                    system.command('systemctl', 'is-active', 'bosun-lavd.service', check=False) == 'active' and
                    'lavd' in system.read('/sys/kernel/sched_ext/root/ops', '')):
                skipped.append('lavd: another scheduler is already attached')
            elif any(system.command('systemctl', 'is-active', u, check=False) == 'active'
                     for u in ['scx.service', 'scx_loader.service', 'scx-loader.service', 'ananicy-cpp.service']):
                skipped.append('lavd: competing scheduler/priority manager active')
            elif not system.fake and '--' + value not in system.command(binpath, '--help'):
                skipped.append('lavd: installed binary lacks --' + value)
            else:
                unit = '[Unit]\nDescription=Bosun experimental LAVD\n[Service]\nType=simple\nExecStart=' + binpath + ' --' + value + '\nRestart=no\n'
                changes.extend([Change(key, '/etc/systemd/system/bosun-lavd.service', unit, 'file'),
                                Change(key, 'bosun-lavd.service', 'active', 'service')])
    if any(k in settings for k in ['zram_algorithm', 'zram_size_percent']):
        generator = system.path('/usr/lib/systemd/system-generators/zram-generator').exists() or system.which('zram-generator')
        active = any(row[0] == '/dev/zram0' for row in swaps)
        generator_owner = system.command('systemctl', 'is-active', 'systemd-zram-setup@zram0.service', check=False) == 'active'
        if not generator or not active or not generator_owner:
            skipped.append('zram_config: requires generator-owned active /dev/zram0; existing swap implementation retained')
        else:
            text = '# Managed by Bosun; takes effect after reboot.\n[zram0]\n'
            algorithm = settings.get('zram_algorithm')
            available = re.sub(r'[\[\]]', '', system.read('/sys/block/zram0/comp_algorithm', '')).split()
            if algorithm and algorithm not in available:
                skipped.append(f'zram_algorithm: {algorithm} unavailable; available: {available}')
            elif algorithm:
                text += f'compression-algorithm = {algorithm}\n'
            if 'zram_size_percent' in settings:
                text += f"zram-size = ram * {settings['zram_size_percent']} / 100\n"
            if 'compression-algorithm' in text or 'zram-size' in text:
                changes.append(Change('zram_config', ZRAM, text, 'file'))
    legacy_lbpw = 'lbpw' in settings and ('file:' + BOOT) in managed
    if bootargs or legacy_lbpw:
        try:
            system.boot_target()
            changes.append(Change('boot', BOOT, boot_content(bootargs), 'file'))
        except BosunError as exc:
            skipped.append('boot: ' + str(exc))
    # Governor first: writing it can also reset EPP. Snapshot/verify all touched values.
    if 'governor' in settings and 'epp' not in settings and not conflicts:
        for p in policies:
            raw = system.read(p + '/energy_performance_preference')
            if raw is not None and any(c.path == p + '/scaling_governor' for c in changes):
                old = managed.get('scalar:' + p + '/energy_performance_preference') if isinstance(managed, dict) else None
                baseline = old['original'] if old else raw
                value = 'performance' if settings['governor'] == 'performance' else baseline
                changes.append(Change('epp', p + '/energy_performance_preference', value))
    changes.sort(key=lambda c: (c.key != 'governor', c.kind == 'service'))
    return changes, [note if note.startswith('SKIP ') else 'SKIP ' + note for note in skipped]


def frequency_expansion(change, before, after):
    if change.key != 'cpu_max_mhz' or before == after:
        return False
    return ((change.path.endswith('/scaling_min_freq') and int(after) < int(before)) or
            (change.path.endswith('/scaling_max_freq') and int(after) > int(before)))


class Engine:
    def __init__(self, system):
        self.sys = system
        self.file = system.path(STATE + '/state.json')
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {
            'schema': 1, 'entries': {}, 'desired': {}, 'profile': 'stock', 'fingerprint': system.fingerprint(),
            'boot_id': system.read('/proc/sys/kernel/random/boot_id', ''), 'pending': None}
        if self.state.get('schema') != 1:
            raise BosunError('Unsupported state schema; update Bosun')

    def save(self):
        atomic(self.file, json.dumps(self.state, indent=2, ensure_ascii=False) + '\n')

    @contextlib.contextmanager
    def locked(self):
        path = self.sys.path(STATE + '/lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.__init__(self.sys)
            yield

    def recover(self):
        self.sys.restore_readonly()
        pending = self.state.get('pending')
        if not pending:
            return []
        conflicts = []
        ordered = list(reversed(pending['writes']))
        def recovery_order(item):
            c = item['change']
            if c['key'] == 'governor':
                return 0
            if c['kind'] == 'service':
                return 4 if item['before'] == 'active' else 1
            if frequency_expansion(Change(**c), c['value'], item['before']):
                return 1
            return 2 if c['kind'] == 'file' else 3
        ordered.sort(key=recovery_order)
        # Check the whole recovery set before a governor can reset a foreign EPP.
        for item in ordered:
            c = Change(**item['change'])
            current = c.current(self.sys)
            if current not in (item['before'], c.value) and not (c.kind == 'service' and current == 'failed'):
                conflicts.append(c.path)
        if conflicts:
            raise BosunError('Interrupted transaction recovery conflicts (state retained): ' + '; '.join(conflicts))
        for item in ordered:
            change = Change(**item['change'])
            current = change.current(self.sys)
            if current == item['before'] and not item.get('reconfigure'):
                continue
            if current != change.value and not item.get('reconfigure') and not (change.kind == 'service' and current == 'failed'):
                conflicts.append(change.path)
                continue
            try:
                self.write(change, item['before'])
            except (OSError, BosunError) as exc:
                conflicts.append(f'{change.path}: {exc}')
        if conflicts:
            raise BosunError('Interrupted transaction recovery conflicts (state retained): ' + '; '.join(conflicts))
        self.state = pending['state_before']
        self.save()
        return ['Recovered interrupted transaction']

    def write(self, change, value):
        if change.path == BOOT:
            with self.sys.writable():
                old = change.current(self.sys)
                try:
                    change.put(self.sys, value)
                    args = re.findall(r'\) ([a-z_.]+=[a-z0-9-]+)"', value or '')
                    self.sys.regenerate_boot(args)
                except BaseException:
                    change.put(self.sys, old)
                    raise
        else:
            try:
                change.put(self.sys, value)
            except OSError as exc:
                if exc.errno != errno.EROFS:
                    raise
                with self.sys.writable():
                    change.put(self.sys, value)
        if change.kind != 'service' and change.current(self.sys) != value:
            raise BosunError(f'{change.path}: write did not produce the requested value; transaction retained for recovery')

    def apply(self, name, settings, dry=False, automatic=False, supersede_session=False):
        validate(settings)
        self.sys.check_device()
        if not dry:
            self.recover()
        dependency_notes = []
        if not dry and not automatic:
            from .dependencies import ensure_for
            dependency_notes = ensure_for(self.sys, settings)
        replay = settings
        if automatic:
            owned = {e['change']['key'] for e in self.state['entries'].values()}
            if 'boot' in owned:
                owned.update(BOOT_KEYS)
            if 'zram_config' in owned:
                owned.update({'zram_algorithm', 'zram_size_percent'})
            replay = {k: v for k, v in settings.items() if k in owned}
        fingerprint = self.sys.fingerprint()
        rebooted = self.state['boot_id'] != self.sys.read('/proc/sys/kernel/random/boot_id', '')
        updated = self.state['fingerprint'] != fingerprint
        planning_entries = self.state['entries']
        if rebooted or updated:
            planning_entries = {ident: {**entry, 'original': Change(**entry['change']).current(self.sys)}
                                if entry['change']['key'] == 'cpu_max_mhz' else entry
                                for ident, entry in planning_entries.items()}
        changes, notes = plan(self.sys, replay, planning_entries)
        notes[:0] = dependency_notes
        if automatic and updated:
            return ['SteamOS/kernel changed: automatic replay paused. Open Bosun, preview and apply to accept new baseline.'], True
        desired = {c.id: c for c in changes}
        lavd_reconfigure = any(c.key == 'lavd' and c.kind == 'file' and c.current(self.sys) != c.value for c in changes)
        requested = set(settings)
        if requested.intersection(BOOT_KEYS):
            requested.add('boot')
        if requested.intersection({'zram_algorithm', 'zram_size_percent'}):
            requested.add('zram_config')
        if 'governor' in requested:
            requested.add('epp')
        entries = self.state['entries']
        actions = []
        blocked = {e['change']['key'] for e in entries.values()
                   if e['change']['kind'] == 'file' and e['change']['key'] == 'lavd'
                   and Change(**e['change']).current(self.sys) != e['last']}
        cpu_blocked = set()
        frequency_blocked = set()
        if not (updated or rebooted):
            for entry in entries.values():
                c = Change(**entry['change'])
                if c.key in {'epp', 'governor'} and c.current(self.sys) != entry['last']:
                    if not (automatic and c.current(self.sys) == entry['original']):
                        cpu_blocked.add(str(Path(c.path).parent))
                if c.key == 'cpu_max_mhz' and c.current(self.sys) != entry['last']:
                    frequency_blocked.add(str(Path(c.path).parent))
            if cpu_blocked:
                notes.append('CONFLICT CPU policy/EPP changed externally; governor and EPP retained together')
            if frequency_blocked:
                notes.append('CONFLICT CPU frequency bounds changed externally; minimum and maximum retained together')
        if blocked:
            notes.append('CONFLICT lavd: owned service unit changed externally; all scheduler operations retained')
        # Removing a setting is a compare-and-restore, never a guessed stock reset.
        for ident, entry in reversed(list(entries.items())):
            if ident in desired:
                continue
            c = Change(**entry['change'])
            if c.key in blocked or (c.key in {'governor', 'epp'} and str(Path(c.path).parent) in cpu_blocked) or (c.key == 'cpu_max_mhz' and str(Path(c.path).parent) in frequency_blocked):
                continue
            if c.key in requested or (automatic and not rebooted):
                notes.append(f'SKIP/RETAIN {c.path}: requested setting currently unavailable; ownership retained')
                continue
            if entry.get('identity', c.identity(self.sys)) != c.identity(self.sys):
                notes.append(f'CONFLICT {c.path}: device identity changed; original retained for manual resolution')
                continue
            current = c.current(self.sys)
            if c.kind == 'scalar' and (updated or rebooted):
                actions.append((c, current, current, 'forget'))
            elif current == entry['last']:
                actions.append((c, current, entry['original'], 'remove'))
            elif current == entry['original']:
                actions.append((c, current, current, 'forget'))
            else:
                notes.append(f'CONFLICT {c.path}: changed outside Bosun; retained, use release to keep external value')
        for ident, c in desired.items():
            current = c.current(self.sys)
            entry = entries.get(ident)
            if c.key in blocked or (c.key in {'governor', 'epp'} and str(Path(c.path).parent) in cpu_blocked) or (c.key == 'cpu_max_mhz' and str(Path(c.path).parent) in frequency_blocked):
                continue
            if entry and entry.get('identity', c.identity(self.sys)) != c.identity(self.sys):
                notes.append(f'CONFLICT {c.path}: device identity changed; use release before applying')
                continue
            if automatic and c.key in POWER:
                if entry and rebooted:
                    actions.append((c, current, current, 'forget'))
                    notes.append(f'POWER EXPIRED {c.path}: new boot; use game launcher to acquire it again')
                if current != c.value:
                    notes.append(f'POWER DIVERGENCE {c.path}: {current}; explicit apply required (Steam/resume ownership)')
                continue
            if entry and (updated or rebooted) and c.kind == 'scalar' and current == c.value:
                actions.append((c, current, current, 'forget'))
                notes.append(f'UNCHANGED {c.path}: new boot baseline already matches; ownership released')
                continue
            service_reset = (c.kind == 'service' and current == 'inactive' and entry and entry['original'] == 'inactive')
            if entry and current != entry['last'] and not service_reset and not ((rebooted or updated) and c.kind == 'scalar'):
                # A known kernel-reset baseline may be restored at resume for non-power settings.
                if not (automatic and c.kind == 'scalar' and current == entry['original']):
                    notes.append(f'CONFLICT {c.path}: actual {current!r}, Bosun last {entry["last"]!r}; use release first')
                    continue
            if not entry and c.kind == 'file' and current is not None:
                notes.append(f'CONFLICT {c.path}: pre-existing file is not owned by Bosun')
                continue
            governor_changes = any(other.key == 'governor' and other.current(self.sys) != other.value for other in changes)
            if current != c.value or (c.key == 'epp' and governor_changes) or (c.key == 'lavd' and c.kind == 'service' and lavd_reconfigure):
                actions.append((c, current, c.value, 'set'))
            elif entry:
                actions.append((c, current, current, 'keep'))
            else:
                notes.append(f'UNCHANGED {c.path}: already {current!r}; no ownership taken')
        actions.sort(key=lambda a: (a[0].key != 'governor', not frequency_expansion(a[0], a[1], a[2])))
        for c, before, after, action in actions:
            notes.append(f'{"PLAN" if dry else "CHANGE"} {c.key}: {c.path}: {before!r} -> {after!r} ({action})')
        if dry:
            return notes, any('CONFLICT ' in n for n in notes)
        oldstate = json.loads(json.dumps(self.state))
        oldstate['pending'] = None
        governor_mutating = any(c.key == 'governor' and before != after for c, before, after, _ in actions)
        writes = [{'change': dataclasses.asdict(dataclasses.replace(c, value=after)), 'before': before,
                   'reconfigure': c.key == 'lavd' and c.kind == 'service' and before == 'active' and lavd_reconfigure}
                  for c, before, after, action in actions if action == 'set' or
                  (action == 'remove' and (before != after or (c.key == 'epp' and governor_mutating)))]
        self.state['pending'] = {'state_before': oldstate, 'writes': writes}
        self.save()
        try:
            # Journal original and intended value before *every* mutation, including removals.
            for c, before, after, action in actions:
                ident = c.id
                if action in {'remove', 'forget'}:
                    if before != after or (c.key == 'epp' and governor_mutating):
                        self.write(c, after)
                    del entries[ident]
                elif action == 'set':
                    entry = entries.get(ident)
                    original = before if not entry or ((updated or rebooted) and c.kind == 'scalar') else entry['original']
                    # Persist original before write, so a power failure cannot lose the baseline.
                    entries[ident] = {'change': dataclasses.asdict(c), 'original': original, 'last': c.value,
                                      'identity': c.identity(self.sys)}
                    self.save()
                    self.write(c, after)
                    if c.kind == 'service' and c.value == 'active' and not self.sys.fake:
                        for _ in range(20):
                            if self.sys.read('/sys/kernel/sched_ext/state') == 'enabled' and 'lavd' in self.sys.read('/sys/kernel/sched_ext/root/ops', ''):
                                break
                            time.sleep(0.1)
                        else:
                            raise BosunError('LAVD did not attach; kernel retained/fell back to fair scheduler')
            if supersede_session and not any('CONFLICT ' in n for n in notes):
                session = self.state.pop('session', None)
                if session:
                    # Commit retirement with the new profile. A delayed launcher exit
                    # must never restore its old baseline or terminate a newer session.
                    self.state['retired_sessions'] = (self.state.get('retired_sessions', []) + [session['token']])[-64:]
            self.state.update(desired=settings, profile=name, fingerprint=fingerprint,
                              boot_id=self.sys.read('/proc/sys/kernel/random/boot_id', ''), pending=None)
            self.save()
        except BaseException:
            self.recover()
            raise
        if any((c.path in {BOOT, ZRAM} or c.key == 'memlock_kb') and before != after for c, before, after, _ in actions):
            notes.append('REBOOT REQUIRED: boot settings change only after reboot (also after rollback).')
        return notes, any('CONFLICT ' in n for n in notes)

    def release(self, key):
        """Explicit keep-external-value resolution; never write a conflicted parameter."""
        if key == 'boot':
            raise BosunError('Boot override cannot be released. Resolve its file conflict and rollback.')
        keys = {'governor', 'epp'} if key in {'governor', 'epp'} else {key}
        if key in {'dirty_background_mib', 'dirty_mib'}:
            keys = {'dirty_background_mib', 'dirty_mib'}
        removed = []
        for ident, entry in list(self.state['entries'].items()):
            if entry['change']['key'] in keys:
                if entry['change']['kind'] != 'scalar':
                    raise BosunError('Only scalar settings can be released')
                removed.append(entry['change']['path'])
                del self.state['entries'][ident]
        for item in keys:
            self.state['desired'].pop(item, None)
            session = self.state.get('session')
            if session:
                session['previous']['desired'].pop(item, None)
                session.get('settings', {}).pop(item, None)
        self.save()
        return removed

    def report(self):
        result = {'system': self.sys.info(), 'profile': self.state['profile'], 'desired': self.state['desired'],
                  'update_pending': self.state['fingerprint'] != self.sys.fingerprint(),
                  'transaction_pending': bool(self.state['pending']), 'session': self.state.get('session'), 'managed': []}
        for entry in self.state['entries'].values():
            c = Change(**entry['change'])
            result['managed'].append({**entry, 'actual': c.current(self.sys), 'matches': c.current(self.sys) == entry['last']})
        return result
