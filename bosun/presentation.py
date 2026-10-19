# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared names and groups for Desktop and Decky; no GUI dependency in the backend."""
LABELS = {
    'governor': 'CPU governor', 'epp': 'CPU EPP', 'boost': 'CPU Boost', 'smt': 'SMT',
    'cpu_max_mhz': 'Предел CPU, MHz', 'tdp_w': 'TDP APU, W', 'gpu_mode': 'GPU policy',
    'mitigations': 'Программные CPU mitigations', 'amd_pstate': 'Режим AMD P-state',
    'lavd': 'Планировщик LAVD', 'nmi_watchdog': 'NMI watchdog', 'soft_watchdog': 'Soft watchdog',
    'mglru': 'MGLRU', 'thp': 'Политика THP', 'thp_defrag': 'Дефрагментация THP',
    'thp_shmem': 'THP для shared memory', 'swappiness': 'Swappiness',
    'zram_page_cluster': 'Swap readahead zram', 'zram_algorithm': 'Компрессор zram',
    'zram_size_percent': 'Размер zram, % RAM', 'compaction_proactiveness': 'Фоновая компактация RAM',
    'dirty_background_mib': 'Фоновый writeback, MiB', 'dirty_mib': 'Прямой writeback, MiB',
    'nvme_scheduler': 'NVMe I/O scheduler', 'sd_scheduler': 'microSD I/O scheduler',
    'nvme_readahead_kb': 'NVMe read-ahead, KiB', 'sd_readahead_kb': 'microSD read-ahead, KiB',
    'home_noatime': 'Отключить atime для /home', 'moverate': 'AMDGPU migration, MB/s',
    'gpu_sched_policy': 'GPU scheduler: 0 RR / 1 FIFO', 'epp_boost': 'Per-core EPP boost',
    'workqueue_power_efficient': 'Энергоэффективные workqueues', 'usb_poll_ms': 'USB HID polling, ms',
    'memlock_kb': 'Лимит закреплённой RAM, KiB', 'ksm': 'Сканирование KSM',
    'sched_autogroup': 'CPU autogroup', 'zswap': 'Zswap поверх zram',
}
GROUPS = ['CPU и питание', 'Память', 'Накопители', 'Загрузка и система', 'Дополнительно']


def group(key):
    if key in {'governor', 'epp', 'boost', 'smt', 'cpu_max_mhz', 'tdp_w', 'gpu_mode', 'lavd', 'sched_autogroup'}:
        return GROUPS[0]
    if key.startswith(('nvme_', 'sd_')) or key == 'home_noatime':
        return GROUPS[2]
    if key in {'mitigations', 'amd_pstate', 'moverate', 'lbpw', 'gpu_sched_policy', 'epp_boost', 'usb_poll_ms', 'workqueue_power_efficient', 'memlock_kb'}:
        return GROUPS[3]
    if key.startswith(('thp', 'zram', 'dirty_', 'khugepaged_', 'watermark_')) or key in {
            'mglru', 'swappiness', 'compaction_proactiveness', 'compact_unevictable', 'extfrag_threshold',
            'min_free_kbytes', 'vfs_cache_pressure', 'page_lock_unfairness', 'zswap', 'swap_vma_readahead', 'ksm', 'vm_stat_interval'}:
        return GROUPS[1]
    return GROUPS[4]
