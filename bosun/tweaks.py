# SPDX-License-Identifier: GPL-3.0-or-later
"""Documented Linux interfaces, bounded independently of distribution version numbers."""
import json
import re

# key: (interface, minimum, maximum, mechanism). No guessed sysfs defaults.
SCALARS = {
    'soft_watchdog': ('/proc/sys/kernel/soft_watchdog', 0, 1, 'Software lockup checking; disabling loses diagnostics.'),
    'hung_task_timeout_secs': ('/proc/sys/kernel/hung_task_timeout_secs', 0, 600, 'Blocked-task scan timeout; 0 disables scans.'),
    'sched_autogroup': ('/proc/sys/kernel/sched_autogroup_enabled', 0, 1, 'Fair scheduler session grouping; workload dependent.'),
    'perf_cpu_time_max_percent': ('/proc/sys/kernel/perf_cpu_time_max_percent', 1, 100, 'Bound CPU overhead of active perf sampling.'),
    'compact_unevictable': ('/proc/sys/vm/compact_unevictable_allowed', 0, 1, 'Allow compaction of locked pages; can introduce minor faults.'),
    'extfrag_threshold': ('/proc/sys/vm/extfrag_threshold', 0, 1000, 'Compaction versus reclaim threshold.'),
    'min_free_kbytes': ('/proc/sys/vm/min_free_kbytes', 16384, 262144, 'Allocator reserve; larger reserves reduce usable RAM.'),
    'watermark_boost_factor': ('/proc/sys/vm/watermark_boost_factor', 0, 15000, 'Extra reclaim after fragmentation.'),
    'watermark_scale_factor': ('/proc/sys/vm/watermark_scale_factor', 10, 500, 'Free-memory watermark spacing; reclaim versus RAM availability.'),
    'vfs_cache_pressure': ('/proc/sys/vm/vfs_cache_pressure', 10, 200, 'Reclaim inode/dentry caches versus page cache.'),
    'page_lock_unfairness': ('/proc/sys/vm/page_lock_unfairness', 1, 32, 'Page-lock handoff versus throughput.'),
    'vm_stat_interval': ('/proc/sys/vm/stat_interval', 1, 60, 'VM counter aggregation interval; longer means less current statistics.'),
    'dirty_expire_centisecs': ('/proc/sys/vm/dirty_expire_centisecs', 500, 6000, 'Age before dirty data becomes eligible for periodic writeback; 100 units = 1 second.'),
    'dirty_writeback_centisecs': ('/proc/sys/vm/dirty_writeback_centisecs', 100, 3000, 'Periodic writeback wake interval; longer delays persistence, 0 is not allowed.'),
    'ksm': ('/sys/kernel/mm/ksm/run', 0, 1, 'Stop or start KSM scanning; existing merged pages are retained when stopped.'),
    'khugepaged_defrag': ('/sys/kernel/mm/transparent_hugepage/khugepaged/defrag', 0, 1, 'Background huge-page compaction.'),
    'khugepaged_pages_to_scan': ('/sys/kernel/mm/transparent_hugepage/khugepaged/pages_to_scan', 256, 8192, 'Pages examined per huge-page scan.'),
    'khugepaged_scan_sleep_ms': ('/sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs', 1000, 60000, 'Pause between huge-page scans.'),
    'khugepaged_alloc_sleep_ms': ('/sys/kernel/mm/transparent_hugepage/khugepaged/alloc_sleep_millisecs', 1000, 120000, 'Pause following huge-page allocation failure.'),
    'khugepaged_max_ptes_none': ('/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none', 0, 511, 'Maximum absent PTEs when collapsing a 2 MiB page.'),
}
QUEUE = {
    'wbt_lat_usec': ('wbt_lat_usec', 0, 100000, 1, 'Writeback latency target; 0 disables throttling.'),
    'rq_affinity': ('rq_affinity', 0, 2, 1, 'Completion CPU affinity.'),
    'add_random': ('add_random', 0, 1, 1, 'Contribute I/O timings to entropy; often already off on flash.'),
    'iostats': ('iostats', 0, 1, 1, 'Block accounting; disabling removes device I/O statistics.'),
    'nr_requests': ('nr_requests', 16, 512, 1, 'Request queue depth; throughput versus latency.'),
    'kyber_read_us': ('iosched/read_lat_nsec', 100, 100000, 1000, 'Kyber read latency target.', 'kyber'),
    'kyber_write_us': ('iosched/write_lat_nsec', 100, 100000, 1000, 'Kyber write latency target.', 'kyber'),
    'bfq_slice_idle_us': ('iosched/slice_idle_us', 0, 20000, 1, 'BFQ idling; throughput versus service guarantees.', 'bfq'),
    'bfq_fifo_expire_sync_ms': ('iosched/fifo_expire_sync', 1, 1000, 1, 'BFQ synchronous expiry.', 'bfq'),
    'bfq_timeout_sync_ms': ('iosched/timeout_sync', 1, 1000, 1, 'BFQ synchronous service timeout.', 'bfq'),
    'bfq_back_seek_penalty': ('iosched/back_seek_penalty', 1, 8, 1, 'BFQ backwards seek cost heuristic.', 'bfq'),
}
STORAGE = {prefix + '_' + key: (prefix, *spec) for prefix in ['nvme', 'sd']
           for key, spec in QUEUE.items() if not (prefix == 'sd' and key.startswith('kyber_'))
           and not (prefix == 'nvme' and key.startswith('bfq_'))}
ZRAM_QUEUE = {
    'zram_block_readahead_kb': ('read_ahead_kb', 0, 128, 'Block read-ahead, separate from swap page-cluster.'),
    'zram_nomerges': ('nomerges', 0, 2, 'Block merge policy; may have no effect on bio-based zram.'),
    'zram_iostats': ('iostats', 0, 1, 'Zram block accounting, not compression statistics.'),
    'zram_add_random': ('add_random', 0, 1, 'Zram I/O entropy contribution; normally already off.'),
}
EXTRA_BOOT = {'amd_pstate', 'gpu_sched_policy', 'epp_boost', 'usb_poll_ms'}
GLOBAL_EXTRA = EXTRA_BOOT | {'home_noatime', 'memlock_kb'}
BOUNDS = {k: (v[1], v[2]) for k, v in SCALARS.items()}
BOUNDS.update({k: (v[2], v[3]) for k, v in STORAGE.items()})
BOUNDS.update({k: (v[1], v[2]) for k, v in ZRAM_QUEUE.items()})
BOUNDS.update({'gpu_sched_policy': (0, 1), 'epp_boost': (0, 1), 'usb_poll_ms': (1, 16),
               'home_noatime': (0, 1), 'memlock_kb': (8192, 2097152),
               'dirty_background_mib': (16, 512), 'dirty_mib': (32, 2048)})
ENUMS = {'amd_pstate': ['active', 'passive', 'guided'],
         'thp_shmem': ['always', 'within_size', 'advise', 'never'], 'swap_vma_readahead': ['on', 'off'],
         'zswap': ['off']}
CATALOG = {k: (f'{lo}..{hi}', SCALARS[k][3] if k in SCALARS else
               STORAGE[k][5] if k in STORAGE else ZRAM_QUEUE[k][3] if k in ZRAM_QUEUE else {
                   'gpu_sched_policy': 'Boot GPU scheduler: 0 round-robin, 1 FIFO; not a CPU scheduler.',
                   'epp_boost': 'Boot per-core EPP boost, only on a kernel exporting this feature.',
                   'usb_poll_ms': 'Boot USB HID polling interval; input latency versus wakeups, not rendering speed.',
                   'home_noatime': 'Remount only /home access-time flags; original flags and mount identity retained.',
                   'memlock_kb': 'Login/reboot-only locked-memory ceiling; does not reserve RAM.',
                   'dirty_background_mib': 'Background writeback threshold; set together with dirty_mib.',
                   'dirty_mib': 'Foreground writeback threshold; set together with dirty_background_mib.',
               }[k]) for k, (lo, hi) in BOUNDS.items()}
CATALOG.update({k: ('|'.join(values), {
    'amd_pstate': 'Boot CPU driver mode; CPPC/kernel support required, reboot then select EPP.',
    'thp_shmem': 'Huge-page policy for shared memory.',
    'swap_vma_readahead': 'VMA versus cluster swap readahead; often redundant with page-cluster=0.',
    'zswap': 'Avoid a second compressed cache when every active swap device is zram; no swapoff.',
}[k]) for k, values in ENUMS.items()})
RUNTIME_KEYS = set(CATALOG) - GLOBAL_EXTRA


def mount_info(system):
    if not system.which('findmnt'):
        return None
    raw = system.command('findmnt', '--json', '--nofsroot', '--target', '/home', '--output', 'TARGET,SOURCE,FSTYPE,VFS-OPTIONS', check=False)
    try:
        info = json.loads(raw)['filesystems'][0]
        if info['target'] == '/home' and info['fstype'] in {'ext4', 'btrfs'} and info.get('source', '').startswith('/dev/') and isinstance(info.get('vfs-options'), str):
            return info
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    return None


def atime_flags(info):
    flags = info['vfs-options'].split(',')
    atime = next((f for f in ['noatime', 'relatime', 'strictatime'] if f in flags), 'strictatime')
    return atime + (',nodiratime' if 'nodiratime' in flags else ',diratime')


def boot_arg(system, key, value):
    if key == 'amd_pstate':
        drivers = [system.read(p + '/scaling_driver', '') for p in system.glob('/sys/devices/system/cpu/cpufreq/policy*')]
        cppc = system.path('/sys/devices/system/cpu/cpu0/acpi_cppc/highest_perf').exists()
        cppc = cppc or 'cppc' in system.read('/proc/cpuinfo', '').split()
        if cppc or any('amd' in d and 'pstate' in d for d in drivers):
            return ['amd_pstate=' + value]
    elif key == 'gpu_sched_policy' and system.path('/sys/module/gpu_sched/parameters/sched_policy').exists():
        return ['gpu_sched.sched_policy=' + str(value)]
    elif key == 'epp_boost' and system.path('/sys/module/amd_pstate/parameters/epp_boost').exists():
        return ['amd_pstate.epp_boost=' + str(value)]
    elif key == 'usb_poll_ms' and system.path('/sys/module/usbhid/parameters/mousepoll').exists():
        return ['usbhid.' + name + '=' + str(value) for name in ['mousepoll', 'kbpoll', 'jspoll']]
    return []


def extra_plan(system, settings):
    from .core import Change, selected
    changes, notes = [], []

    def add(key, path, value):
        if system.path(path).exists():
            changes.append(Change(key, path, str(value)))
        else:
            notes.append(f'SKIP {key}: interface missing: {path}')

    for key, value in settings.items():
        if key in SCALARS:
            path = SCALARS[key][0]
            if key == 'min_free_kbytes':
                match = re.search(r'MemTotal:\s+(\d+)', system.read('/proc/meminfo', ''))
                if not match or value > int(match[1]) // 64:
                    notes.append('SKIP min_free_kbytes: reserve exceeds 1/64 of detected RAM, or RAM unavailable')
                    continue
            add(key, path, value)
        elif key in STORAGE:
            prefix, suffix, _, _, scale, *_ = STORAGE[key]
            pattern = 'nvme*n*' if prefix == 'nvme' else 'mmcblk[0-9]*'
            devices = system.glob('/sys/block/' + pattern)
            for device in devices:
                if prefix == 'sd' and system.read(device + '/device/type') != 'SD':
                    notes.append(f'SKIP {key}: {device} is not an SD card')
                    continue
                spec = STORAGE[key]
                scheduler = spec[6] if len(spec) > 6 else None
                current = selected(system.read(device + '/queue/scheduler', ''))
                if scheduler and current != scheduler:
                    notes.append(f'SKIP {key}: select {scheduler} first, then apply its tunables')
                    continue
                add(key, device + '/queue/' + suffix, value * scale)
            if not devices:
                notes.append(f'SKIP {key}: no matching storage')
        elif key in ZRAM_QUEUE:
            paths = system.glob('/sys/block/zram*/queue/' + ZRAM_QUEUE[key][0])
            if not paths:
                notes.append(f'SKIP {key}: zram queue interface unavailable')
            for path in paths:
                add(key, path, value)
        elif key == 'thp_shmem':
            path = '/sys/kernel/mm/transparent_hugepage/shmem_enabled'
            if value in re.sub(r'[\[\]]', '', system.read(path, '')).split():
                add(key, path, value)
            else:
                notes.append('SKIP thp_shmem: requested policy unavailable')
        elif key == 'swap_vma_readahead':
            add(key, '/sys/kernel/mm/swap/vma_ra_enabled', 'true' if value == 'on' else 'false')
        elif key == 'zswap':
            swaps = [line.split()[0] for line in system.read('/proc/swaps', '').splitlines()[1:] if line.split()]
            if swaps and all(path.startswith('/dev/zram') for path in swaps):
                add(key, '/sys/module/zswap/parameters/enabled', 'N')
            else:
                notes.append('SKIP zswap: only disabled when all active swap devices are zram')
        elif key in {'dirty_background_mib', 'dirty_mib'}:
            stem = 'dirty_background' if key == 'dirty_background_mib' else 'dirty'
            path = '/proc/sys/vm/' + stem
            if all(system.path(path + tail).exists() for tail in ['_bytes', '_ratio']):
                changes.append(Change(key, path + '_bytes', json.dumps([value * 1024 * 1024, 0])))
            else:
                notes.append(f'SKIP {key}: writeback interface unavailable')
        elif key == 'home_noatime':
            info = mount_info(system)
            if not info:
                notes.append('SKIP home_noatime: requires a separate ext4/btrfs /home mount and findmnt')
            else:
                changes.append(Change(key, '/home', 'noatime,nodiratime' if value else 'relatime,diratime'))
        elif key == 'memlock_kb':
            try:
                manifest = json.loads(system.read('/home/.bosun/install.json', '{}'))
                import pwd
                user = pwd.getpwnam(manifest['user'])
                if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', manifest['user']) or user.pw_uid == 0:
                    raise ValueError('desktop user required')
                limit = str(value * 1024)
                files = {
                    '/etc/security/limits.d/90-bosun.conf': f'{manifest["user"]} soft memlock {value}\n{manifest["user"]} hard memlock {value}\n',
                    '/etc/systemd/user.conf.d/90-bosun.conf': f'[Manager]\nDefaultLimitMEMLOCK={limit}\n',
                    f'/etc/systemd/system/user@{user.pw_uid}.service.d/90-bosun.conf': f'[Service]\nLimitMEMLOCK={limit}\n',
                }
                changes.extend(Change(key, path, text, 'file') for path, text in files.items())
            except (KeyError, ValueError):
                notes.append('SKIP memlock_kb: installed desktop user unavailable')
    return changes, notes
