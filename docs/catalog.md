# Полный каталог Bosun 0.3
Сгенерировано из `bosun.core.CATALOG`. Все параметры доступны по отдельности через профиль/Decky; наличие ключа не отменяет capability check. Runtime-параметры можно применять глобально или временно на время игры. Boot/глобальные параметры запрещены в игровом overlay.
| Ключ | Значения | Область | Механизм / компромисс |
|---|---|---|---|
| `amd_pstate` | active, passive, guided | boot, глобально | Boot CPU driver mode; CPPC/kernel support required, reboot then select EPP. |
| `boost` | 0, 1 | runtime / профиль игры | Per-game CPU boost; disabling can free APU budget, can hurt CPU-bound games. |
| `compact_unevictable` | 0..1 | runtime / профиль игры | Allow compaction of locked pages; can introduce minor faults. |
| `compaction_proactiveness` | 0..100 | runtime / профиль игры | Experimental background memory compaction; 0 removes proactive work but may increase fragmentation. |
| `cpu_max_mhz` | 400..3500 | runtime / профиль игры | CPUFreq policy ceiling; hardware bounds checked, min lowered only if required. Verify actual clocks under load; not a guaranteed SMU/Boost hard cap. |
| `dirty_background_mib` | 16..512 | runtime / профиль игры | Background writeback threshold; set together with dirty_mib. |
| `dirty_expire_centisecs` | 500..6000 | runtime / профиль игры | Age before dirty data becomes eligible for periodic writeback; 100 units = 1 second. |
| `dirty_mib` | 32..2048 | runtime / профиль игры | Foreground writeback threshold; set together with dirty_background_mib. |
| `dirty_writeback_centisecs` | 100..3000 | runtime / профиль игры | Periodic writeback wake interval; longer delays persistence, 0 is not allowed. |
| `epp` | available EPP name | runtime / профиль игры | Per-game CPPC energy hint; may reduce CPU-bound FPS/lows. |
| `epp_boost` | 0..1 | boot, глобально | Boot per-core EPP boost, only on a kernel exporting this feature. |
| `extfrag_threshold` | 0..1000 | runtime / профиль игры | Compaction versus reclaim threshold. |
| `governor` | available governor | runtime / профиль игры | Per-game CPU frequency policy; driver-specific. |
| `gpu_mode` | auto, low, high | runtime / профиль игры | AMDGPU dynamic clock policy; low/high may badly redistribute budget. |
| `gpu_sched_policy` | 0..1 | boot, глобально | Boot GPU scheduler: 0 round-robin, 1 FIFO; not a CPU scheduler. |
| `home_noatime` | 0..1 | глобально; runtime remount | Remount only /home access-time flags; original flags and mount identity retained. |
| `hung_task_timeout_secs` | 0..600 | runtime / профиль игры | Blocked-task scan timeout; 0 disables scans. |
| `khugepaged_alloc_sleep_ms` | 1000..120000 | runtime / профиль игры | Pause following huge-page allocation failure. |
| `khugepaged_defrag` | 0..1 | runtime / профиль игры | Background huge-page compaction. |
| `khugepaged_max_ptes_none` | 0..511 | runtime / профиль игры | Maximum absent PTEs when collapsing a 2 MiB page. |
| `khugepaged_pages_to_scan` | 256..8192 | runtime / профиль игры | Pages examined per huge-page scan. |
| `khugepaged_scan_sleep_ms` | 1000..60000 | runtime / профиль игры | Pause between huge-page scans. |
| `ksm` | 0..1 | runtime / профиль игры | Stop or start KSM scanning; existing merged pages are retained when stopped. |
| `lavd` | autopilot, balanced, powersave, performance | глобально; scheduler service | Experimental installed scx_lavd mode; kernel/BTF required, no other scx owner. |
| `lbpw` | -1, 0, 1 | legacy key; SKIP на Deck | Legacy compatibility key; skipped on Deck RDNA2 because upstream LBPW is GFX9-specific. |
| `memlock_kb` | 8192..2097152 | глобально; reboot/login | Login/reboot-only locked-memory ceiling; does not reserve RAM. |
| `mglru` | on, full, off | runtime / профиль игры | on changes main bit; full enables all three documented components and preserves other bits. |
| `min_free_kbytes` | 16384..262144 | runtime / профиль игры | Allocator reserve; larger reserves reduce usable RAM. |
| `mitigations` | off | boot, глобально | Boot: disable software CPU vulnerability mitigations; reboot required. |
| `moverate` | 8..1024 | boot, глобально | Experimental boot AMDGPU migration rate MB/s; version-dependent. |
| `nmi_watchdog` | 0, 1 | runtime / профиль игры | Hard-lockup detector perf interrupt; 0 removes it, reduces hang diagnostics. |
| `nvme_add_random` | 0..1 | runtime / профиль игры | Contribute I/O timings to entropy; often already off on flash. |
| `nvme_iostats` | 0..1 | runtime / профиль игры | Block accounting; disabling removes device I/O statistics. |
| `nvme_kyber_read_us` | 100..100000 | runtime / профиль игры | Kyber read latency target. |
| `nvme_kyber_write_us` | 100..100000 | runtime / профиль игры | Kyber write latency target. |
| `nvme_nr_requests` | 16..512 | runtime / профиль игры | Request queue depth; throughput versus latency. |
| `nvme_readahead_kb` | 0..4096 | runtime / профиль игры | Experimental NVMe read-ahead; loading/streaming vs unnecessary reads. |
| `nvme_rq_affinity` | 0..2 | runtime / профиль игры | Completion CPU affinity. |
| `nvme_scheduler` | available scheduler | runtime / профиль игры | Experimental storage latency/throughput tradeoff. |
| `nvme_wbt_lat_usec` | 0..100000 | runtime / профиль игры | Writeback latency target; 0 disables throttling. |
| `page_lock_unfairness` | 1..32 | runtime / профиль игры | Page-lock handoff versus throughput. |
| `perf_cpu_time_max_percent` | 1..100 | runtime / профиль игры | Bound CPU overhead of active perf sampling. |
| `sched_autogroup` | 0..1 | runtime / профиль игры | Fair scheduler session grouping; workload dependent. |
| `sd_add_random` | 0..1 | runtime / профиль игры | Contribute I/O timings to entropy; often already off on flash. |
| `sd_bfq_back_seek_penalty` | 1..8 | runtime / профиль игры | BFQ backwards seek cost heuristic. |
| `sd_bfq_fifo_expire_sync_ms` | 1..1000 | runtime / профиль игры | BFQ synchronous expiry. |
| `sd_bfq_slice_idle_us` | 0..20000 | runtime / профиль игры | BFQ idling; throughput versus service guarantees. |
| `sd_bfq_timeout_sync_ms` | 1..1000 | runtime / профиль игры | BFQ synchronous service timeout. |
| `sd_iostats` | 0..1 | runtime / профиль игры | Block accounting; disabling removes device I/O statistics. |
| `sd_nr_requests` | 16..512 | runtime / профиль игры | Request queue depth; throughput versus latency. |
| `sd_readahead_kb` | 0..4096 | runtime / профиль игры | Experimental SD read-ahead; storage workload-specific. |
| `sd_rq_affinity` | 0..2 | runtime / профиль игры | Completion CPU affinity. |
| `sd_scheduler` | available scheduler | runtime / профиль игры | Experimental microSD scheduler; detected by device type. |
| `sd_wbt_lat_usec` | 0..100000 | runtime / профиль игры | Writeback latency target; 0 disables throttling. |
| `smt` | on, off | глобально; runtime CPU topology | Experimental CPU topology; can hurt threaded games. Never forceoff. |
| `soft_watchdog` | 0..1 | runtime / профиль игры | Software lockup checking; disabling loses diagnostics. |
| `swap_vma_readahead` | on, off | runtime / профиль игры | VMA versus cluster swap readahead; often redundant with page-cluster=0. |
| `swappiness` | 0..200 | runtime / профиль игры | Experimental relative reclaim cost; >100 requires active zram here. |
| `tdp_w` | 4..15 | runtime / профиль игры | Per-game APU cap in watts, NOT total battery power. |
| `thp` | always, madvise, never | runtime / профиль игры | Experimental huge-page allocation policy. |
| `thp_defrag` | always, defer, defer+madvise, madvise, never | runtime / профиль игры | Allocation compaction policy; stalls vs huge-page coverage. |
| `thp_shmem` | always, within_size, advise, never | runtime / профиль игры | Huge-page policy for shared memory. |
| `usb_poll_ms` | 1..16 | boot, глобально | Boot USB HID polling interval; input latency versus wakeups, not rendering speed. |
| `vfs_cache_pressure` | 10..200 | runtime / профиль игры | Reclaim inode/dentry caches versus page cache. |
| `vm_stat_interval` | 1..60 | runtime / профиль игры | VM counter aggregation interval; longer means less current statistics. |
| `watermark_boost_factor` | 0..15000 | runtime / профиль игры | Extra reclaim after fragmentation. |
| `watermark_scale_factor` | 10..500 | runtime / профиль игры | Free-memory watermark spacing; reclaim versus RAM availability. |
| `workqueue_power_efficient` | 0, 1 | boot, глобально | Experimental boot WQ_POWER_EFFICIENT consolidation; may trade latency for fewer wakeups. |
| `zram_add_random` | 0..1 | runtime / профиль игры | Zram I/O entropy contribution; normally already off. |
| `zram_algorithm` | lz4, lzo-rle, zstd | глобально; reboot | Reboot-only compressor override; never live swapoff. |
| `zram_block_readahead_kb` | 0..128 | runtime / профиль игры | Block read-ahead, separate from swap page-cluster. |
| `zram_iostats` | 0..1 | runtime / профиль игры | Zram block accounting, not compression statistics. |
| `zram_nomerges` | 0..2 | runtime / профиль игры | Block merge policy; may have no effect on bio-based zram. |
| `zram_page_cluster` | 0..3 | runtime / профиль игры | Swap readahead exponent; only when highest-priority swap is zram. |
| `zram_size_percent` | 25..200 | глобально; reboot | Reboot-only virtual zram capacity as percent of RAM, not RAM reservation. |
| `zswap` | off | runtime / профиль игры | Avoid a second compressed cache when every active swap device is zram; no swapoff. |

Значения `dirty_background_mib` и `dirty_mib` задаются вместе; background < dirty. Сохраняются оба kernel-режима bytes/ratio. Kyber/BFQ-specific tunables применяются после выбора соответствующего scheduler отдельным применением. `min_free_kbytes` дополнительно ограничен 1/64 фактической RAM. Memlock — лимит, не резервирование памяти.

Launch options (процесс игры, не sysctl): `cache_gb` 1–16, `sync` auto/fsync, `dxvk_latency` 1–4, `radv_wave32` compute/pixel/geometry/all, `radv_nir_cache` true, `io_priority` 0–7 best-effort, `affinity` physical. Их удобный редактор находится в Decky. Исходные строки с конфликтующими env variables/compound shell syntax пресеты не переписывают; доступно ручное редактирование с backup.

Ожидаемые эффекты и решения по исключённым методам: [global-analysis.md](global-analysis.md). Ниже нет измеренного процента прироста: для всех кандидатов требуется A/B на устройстве.
