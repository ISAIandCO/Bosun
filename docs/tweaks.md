# Выбор оптимизаций и технические основания

Дата проверки первичных источников: 18 октября 2026. Приложенный research report прочитан, но его номера SteamOS, датировки и рекомендации не используются как спецификация совместимости. Bosun определяет DMI Jupiter/Galileo, ID SteamOS, ядро, CPU driver, доступные policy choices и интерфейсы текущего устройства. Наличие интерфейса — необходимое условие записи, **не доказательство аппаратной валидации или выигрыша**.

В Bosun нет чисел измеренного прироста на физических Deck. Ниже «механизм» означает поведение по документации/исходникам, «кандидат» — обоснованный для A/B способ, результат которого зависит от нагрузки.

## Реализованные методы

| Параметр Bosun | Механизм и применимость | Ожидаемая польза / возможная регрессия | Статус |
|---|---|---|---|
| `mitigations=off` | Общий параметр ядра отключает поддерживаемые программные mitigations. Управляется override GRUB; фактическое состояние смотрите в `/proc/cmdline` и CPU vulnerabilities. | Возможен выигрыш в CPU/kernel/syscall-heavy сценах. В GPU-bound сцене может не проявиться. Снижает ИБ; это разрешённый компромисс, отдельного повторного разрешения не требуется. | Рекомендуемый; reboot |
| `nmi_watchdog=0` | Выключает hard lockup detector, использующий perf/NMI. Soft watchdog и аппаратные термозащиты не выключаются. | Уменьшение постоянной диагностической работы, обычно небольшой или неразличимый эффект. Потеря hard-lockup diagnostics. | Рекомендуемый при экспортируемом sysctl |
| `zram_page_cluster=0` | Читается одна нужная swap-страница без readahead. Включается только если swap максимального приоритета исключительно `/dev/zram*`. Нижележащий disk swap не удаляется. | Уменьшение лишней декомпрессии/первой задержки; возможны дополнительные faults при последовательном доступе. Нужен реальный swap workload. | Рекомендуемый условно |
| `mglru=on` | В `/sys/kernel/mm/lru_gen/enabled` ставится только бит 0, оптимизации других битов сохраняются. `off` очищает только бит 0. | Более подходящий reclaim при memory pressure. Бессмысленно обещать FPS, если память не испытывает давления; может уже быть включён. | Рекомендуемый; не тюнинг внутренних эвристик |
| `thp_defrag=defer+madvise` | Обычные области инициируют фоновую reclaim/compaction; для `MADV_HUGEPAGE` остаётся прямой путь. Политика выделения THP не меняется. | Кандидат на меньше stalls относительно `always`; относительно `madvise` добавляет фоновую работу. Может увеличить CPU cost. | Рекомендуемый стартовый A/B; можно отключить независимо |
| `governor`, `epp` | Governor из `scaling_available_governors`; EPP только active amd-pstate-epp, powersave и реально доступное именованное значение. | В active pstate powersave позволяет автономное управление, EPP задаёт предпочтение CPU performance↔energy. Может освободить бюджет GPU, но ухудшить CPU-bound lows. | Профиль игры |
| `boost` | Общий cpufreq boost, иначе per-policy boost при наличии. | Убирает дорогие CPU bursts в игре с запасом CPU; может ухудшить FPS/frametime. | Профиль игры |
| `cpu_max_mhz` | Верхняя граница CPU 400–3500 MHz, в пределах runtime scaling_min и cpuinfo_max. | Распределение бюджета CPU↔GPU; цель только при сохранении нужного FPS. Занижение частоты само по себе не считается успехом. | Профиль игры |
| `tdp_w` | Microwatt power caps только в hwmon с именем amdgpu/steamdeck; границы 3–15 W дополнительно ограничены min/max драйвера. | Поиск точки эффективности; снижение TDP с потерей целевого FPS не считается оптимизацией. Это лимит APU, не всей батареи. | Профиль игры |
| `gpu_mode` | Штатный `power_dpm_force_performance_level` auto/low/high на единственном AMD GPU; с eGPU пропуск. | Перераспределение частот/бюджета. High может отобрать мощность у CPU, low — сильно затормозить GPU. | Эксперимент игры |
| `smt` | Штатный smt/control on/off. `forceoff`, offline core masks не используются. | Может помочь отдельным мало-поточным нагрузкам, ухудшить многопоточные. Меняется глобально между играми, не игровым launcher. | Эксперимент |
| `swappiness` | 0–200; >100 только при преимущественном zram. Не изменяет swap device, размер или compressor. | Изменение стоимости anonymous/file reclaim. 150 — кандидат, не универсальное оптимальное значение. | Эксперимент при memory PSI |
| `thp` / `thp_defrag` | Доступные значения соответствующих sysfs; enabled отдельно от defrag. | THP может уменьшить TLB overhead, но выделение и compaction могут добавлять задержки. | Кроме указанного recommended defrag — эксперимент |
| `nvme_scheduler` / `sd_scheduler` | Только уже доступный I/O scheduler; NVMe по device type, SD только с `device/type=SD`, eMMC не принимается за SD. Модули не подгружаются принудительно. | Latency vs throughput при streaming/contention; BFQ не бесплатен и может снизить throughput. | Эксперимент сцены; значения не зашиты в recommended |
| `moverate` | `amdgpu.moverate=N` только при наличии exported module parameter, boot override без initramfs replacement. | Максимальная скорость BO migration; смысл зависит от ветки TTM/AMDGPU и workload. Значение 256 — тест, не обещание. | Эксперимент, reboot |
| `lbpw` | `amdgpu.lbpw=-1/0/1` только при наличии параметра. | Отключение balancing per watt может повысить потребление, что противоречит цели без компенсации FPS. Наличие export не гарантирует, что конкретный ASIC использует алгоритм. | Эксперимент, reboot |
| `lavd=autopilot` | Собственный systemd service с установленным scx_lavd, sched_ext/BTF, проверкой attach и конфликтующих managers. Сервис имеет смысл только пока BPF scheduler загружен; Bosun не опрашивает его постоянно. | Меньше scheduler-induced latency; autopilot управляет power policy самого scheduler. Есть upstream Deck-демонстрации, но переносить их эффект на ваш kernel/игру нельзя. | Приоритетный эксперимент |

## Что найдено/уточнено дополнительно к отчёту

* **Swap readahead на zram**: документация ядра прямо описывает `page-cluster=0` и компромисс initial fault latency / subsequent faults. Поэтому это отдельный условный runtime-твик; глобальная замена zram-конфига не нужна.
* **Связь governor с EPP**: смена governor может сбросить EPP. Bosun сохраняет оба значения, применяет governor раньше EPP и возвращает их в том же порядке зависимости. Проверяется доступность preferences, а не строка `power` из чужого скрипта.
* **LAVD, Linux Plumbers 2024**: первичный доклад разработчиков содержит Steam Deck сценарий и объясняет autopilot/compaction. Это делает LAVD содержательным кандидатом, но не заменяет современные LCD/OLED A/B.
* **Реальный SteamOS GRUB target**: исходник `update-grub.in` пишет в EFI, а не обязательно в `/boot/grub/grub.cfg`. Bosun определяет destination по установленному wrapper и отказывается угадывать другую схему. Создаётся проверенный временный конфиг и сохраняется recovery-copy текущего.

## Оценённые, но не включённые методы

* **Смена ядра**: допустима по условию задачи, но без контролируемого выигрыша на точной SteamOS не оправдывает замену целой ветки, scheduler и power limits одновременно. `sched_ext` позволяет исследовать scheduler отдельно. Нужен отдельный образ/boot entry и аппаратные тесты; готовых «обещаний» в коде нет.
* **Замена Mesa/Gamescope**: в исходниках SDWEAK установщик включает фиксированные бинарные пакеты и удаление gamemode через `-Rdd`; это не переносится в Bosun. Выгода должна быть установлена для конкретного дефекта и совпадающих версий. Никакого downgrade, удаления зависимостей или изменения pacman trust.
* **Размер/компрессор zram, swapfile, zswap поверх zram**: не делаем live swapoff/restart с риском OOM и потери работы. Стандартный zram остаётся; сначала смотрим PSI/compression/major faults. Само увеличение виртуальной ёмкости не добавляет RAM и не является измеренным улучшением.
* **MGLRU bitmask 7, THP always, dirty/watermark/min_free наборы**: не копируем пакетно. Осмысленные отдельные интерфейсы реализованы выше; сомнительные универсальные значения не применяем.
* **GPU fixed MHz**: pp_od_clk_voltage на Deck может быть пустым. Восстановление скрытого range догадкой нарушило бы обещание точного отката. Есть реальный GPU policy и штатные средства Steam для ручного clock.
* **UMA 4 GB**: не увеличивает физическую память, может быть workaround конкретной игры; автоматизация BIOS без надёжного exact-state snapshot здесь не оправдана.
* **Undervolt**: потенциально полезен для perf/W, но требует индивидуального подбора и проверки стабильности. Нет универсального безопасного значения и автоматизированного firmware-отката, поэтому не заявляется как функция.
* **USB polling 1 ms, LCD 70 Hz**: потребление/latency/панельные ограничения; нет основания применять ко всем встроенным устройствам и OLED. FPS через изменение качества/разрешения также не считается системным эффектом.
* **noatime / NVMe APST / Wi-Fi powersave / выключение фоновых служб**: эффект workload/device-specific, есть риски загрузки/ввода/сети или latency. Без воспроизводимого Deck bottleneck не добавляют обоснованный default. NMI detector — узкий отдельный overhead-твик, а не отключение всей диагностики.

## Первичные источники

1. [Linux AMD P-State](https://docs.kernel.org/admin-guide/pm/amd-pstate.html) — active/passive modes, EPP, governors, boost.
2. [Linux VM sysctl](https://docs.kernel.org/admin-guide/sysctl/vm.html) — swappiness 0–200, zram, page-cluster и cost tradeoffs.
3. [Transparent Hugepages](https://docs.kernel.org/admin-guide/mm/transhuge.html) — точные различия defer/madvise/defer+madvise/always/never.
4. [Multi-Gen LRU](https://docs.kernel.org/admin-guide/mm/multigen_lru.html) — смысл главного/дополнительных битов.
5. [AMDGPU module parameters](https://docs.kernel.org/gpu/amdgpu/module-parameters.html) и [GPU power/thermal interfaces](https://docs.kernel.org/gpu/amdgpu/thermal.html) — параметры драйвера и policy, наличие алгоритма нужно проверять по ветке/ASIC.
6. [Kernel boot parameters](https://docs.kernel.org/admin-guide/kernel-parameters.html) — mitigations.
7. [Lockup detectors](https://docs.kernel.org/admin-guide/lockup-watchdogs.html) — NMI/perf и потеря диагностического механизма.
8. [BFQ](https://docs.kernel.org/block/bfq-iosched.html) — latency/throughput/CPU overhead.
9. [sched_ext](https://docs.kernel.org/scheduler/sched-ext.html), [scx_lavd](https://github.com/sched-ext/scx/tree/main/scheds/rust/scx_lavd), [LPC 2024: LAVD](https://lpc.events/event/18/contributions/1713/attachments/1425/3058/scx_lavd-lpc-mc-24.pdf) — реализация, fallback, Deck-пример.
10. [SteamOS customizations, upstream source mirror](https://github.com/evlaV/steamos-customizations/blob/master/grub/bin/update-grub.in) — EFI output path; [grub integration](https://github.com/evlaV/steamos-customizations/blob/master/grub-integration.md). Код Valve/Collabora в зеркале, локальный установленный wrapper остаётся решающим.
11. [SimpleDeckyTDP Steam Deck adapter](https://github.com/aarron-lee/SimpleDeckyTDP/blob/main/py_modules/devices/steam_deck.py), [GPU implementation](https://github.com/aarron-lee/SimpleDeckyTDP/blob/main/py_modules/gpu_utils.py) — практические Deck interfaces, пустой pp_od_clk_voltage.
12. [SDWEAK installer](https://github.com/Taskerer/SDWEAK/blob/main/install.sh) — исследован фактический код, не скопирован целый набор значений/пакетов.
13. [MangoHud](https://github.com/flightlessmango/MangoHud) — raw frame logging, log_interval, output_folder и launch options.
14. [systemd.path](https://www.freedesktop.org/software/systemd/man/latest/systemd.path.html) — событийный game-session helper без polling daemon.

Ссылки на движущиеся ветки нужны для проверки механизма, не являются version pin для системы пользователя. Bosun не устанавливает из них пакеты автоматически.
