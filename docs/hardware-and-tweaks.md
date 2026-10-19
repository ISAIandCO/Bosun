# LCD/OLED, дополнительный отбор методов и Bosun 0.3

Проверка первичных источников: 18 октября 2026. Механизм по документации/исходникам отделён от аппаратного результата: измерений FPS, 1% low или Wh на физических Deck пока нет.

## Аппаратная основа

| Свойство | LCD / Jupiter | OLED / Galileo | Значение для выбора твиков |
|---|---|---|---|
| APU | 7 nm, Zen 2 4c/8t, 8 RDNA2 CU | 6 nm, Zen 2 4c/8t, 8 RDNA2 CU | Не разные поколения CPU; более новый техпроцесс не означает одинаковую кривую эффективности |
| CPU/GPU верхние частоты | CPU до 3.5 GHz, GPU до 1.6 GHz | Те же верхние частоты | Нет основания автоматически разгонять OLED или переносить стабильный undervolt с другого экземпляра |
| Общая память | 16 GB LPDDR5, 5500 MT/s | 16 GB LPDDR5, 6400 MT/s | CPU и GPU делят физическую память; одинаковый объём не означает одинаковые memory-bound результаты |
| Теоретическая пропускная способность | 88 GB/s | 102.4 GB/s | Расчёт MT/s × 128 бит / 8, не измеренная скорость игры. Эффект memory/BO migration твика может различаться |
| Батарея | 40 Wh design | 50 Wh design | Большая батарея сама увеличивает время игры; сравнивать perf/W по реальной мощности, а не по минутам между разными Deck |
| Панель | 1280×800 LCD, до 60 Hz | 1280×800 OLED, до 90 Hz, HDR | Cap/refresh/яркость/HDR фиксировать. Например, 30 FPS кратны 60/90 Hz, 40 FPS — 40/80 Hz, 45 FPS — 90 Hz; это выбор pacing, не системный прирост |
| Накопитель | Включая ранние 64 GB eMMC и NVMe | NVMe | `mmcblk` не всегда microSD: Bosun проверяет `device/type=SD`, eMMC не получает SD scheduler/read-ahead |
| microSD | UHS-I | UHS-I | Не обещать, что OLED-контроллер превращает карту в SSD; нужны streaming-тесты |
| Беспроводная связь | Wi-Fi 5, Bluetooth 5.0 | Wi-Fi 6E, Bluetooth 5.3 | Глобальные Wi-Fi/USB power-saving могут ухудшить latency/стабильность; не добавлены в recommended |

Источник аппаратных чисел: [Valve LCD specs](https://www.steamdeck.com/en/tech/deck), [Valve OLED specs](https://www.steamdeck.com/en/tech/oled). Панельные параметры не меняются Bosun и выводятся как сведения о модели, не как runtime-измерение. Активный внешний монитор может иметь другие режимы.

SteamOS — Arch-based система с read-only root, отдельным writable `/home`, собственным kernel/AMDGPU/Mesa/Gamescope и профилями Steam. Номер версии сам по себе не гарантирует интерфейс: Bosun читает текущие драйверы и sysfs. После изменения OS/kernel/Mesa/Gamescope автоматическое системное replay приостанавливается до явного принятия нового baseline.

В 0.3 дополнительные методы уже реализованы: [глобальный отбор и SDWEAK comparison](global-analysis.md), [полный каталог](catalog.md). Таблица ниже описывает первоначальные отдельные эксперименты; она не ограничивает общий набор.

## Отдельные эксперименты

| Ключ | Реальное действие | Предполагаемый эффект | Возможная регрессия |
|---|---|---|---|
| `compaction_proactiveness` 0–100 | `/proc/sys/vm/compaction_proactiveness` | 0 убирает proactive background compaction, может уменьшить CPU cost/latency spikes | Больше фрагментации; позднее выделение больших страниц может стать дороже. Не выключает все пути compaction |
| `nvme_readahead_kb` 0–4096 | `queue/read_ahead_kb` только NVMe | Меньше лишнего чтения или более эффективный последовательный streaming при подборе значения | Слишком малое значение ухудшает последовательное чтение, большое расходует память и I/O |
| `sd_readahead_kb` 0–4096 | Такой же параметр только реальной SD | Возможное улучшение loading/streaming на конкретной карте | Нет универсального оптимума для карт и игр |
| `zram_algorithm` lz4/lzo-rle/zstd | Отдельный `/etc/systemd/zram-generator.conf.d/90-bosun.conf`, только при активном generator-owned zram0 и наличии алгоритма | Обмен CPU compression cost на плотность хранения; lz4 может быть дешевле по CPU, zstd может лучше экономить RAM | При давлении памяти худший баланс может увеличить stalls; эффект зависит от данных |
| `zram_size_percent` 25–200 | Размер виртуального zram как процент RAM через тот же override | Больше пространства для сжимаемых холодных страниц в memory-heavy игре | Виртуальная ёмкость не является дополнительной физической RAM и не предотвращает любой OOM |
| `workqueue_power_efficient` 0/1 | Boot `workqueue.power_efficient=1` для очередей с флагом WQ_POWER_EFFICIENT | Консолидация работы вместо дополнительных пробуждений CPU; кандидат для capped/light workloads | Дополнительная миграция/latency/cache cost; эффект зависит от того, какие очереди использует текущая ветка ядра |

Новые параметры **не добавлены массово в recommended**. Их можно выбрать в Decky или CLI и сравнить по одному. Zram и workqueue требуют reboot; при rollback тоже может требоваться reboot. Bosun не делает live swapoff, не сбрасывает zram во время игры и не заменяет vendor config в `/usr/lib`. Откат zram удаляет только свой override, возвращая текущую конфигурацию поставщика/других инструментов.

Примеры для независимого A/B:

```bash
bosun profile-save compaction-test --base recommended --set compaction_proactiveness=0
bosun profile-save sd-streaming-test --base recommended --set sd_readahead_kb=256
bosun profile-save zram-test --base recommended --set zram_algorithm=lz4
bosun profile-save workqueue-test --base recommended --set workqueue_power_efficient=1
```

Числа выше — кандидаты, не результаты оптимизации. Для eMMC нет автоматического отождествления с microSD. Для OLED не ставятся LCD-патчи панели.

## Другие выводы поиска

* **LBPW пересмотрен и отключён для Deck:** ранее он был доступен как эксперимент по наличию exported parameter. Проверка [gfx_v9_0.c](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/gfx_v9_0.c) показывает применение для IP 9.1.0/9.2.2/9.4.0. Это не графика RDNA2 Deck. Теперь ключ принимается для совместимости старых профилей, но явно пропускается; при повторном применении убирается старый флаг из принадлежащего Bosun override.
* **Moverate уточнён:** [amdgpu_device.c](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/amdgpu_device.c) переводит значение через ilog2, а [amdgpu_cs.c](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/amdgpu_cs.c) использует его для накопленного бюджета миграции перед submission. Это не увеличение физической пропускной способности RAM. Примеры степеней двойки удобнее для интерпретации, но эффект всё равно требует VRAM-pressure теста.
* **LAVD не стал default:** в upstream есть сообщения о регрессиях frametime разных версий, в частности [scx #3750](https://github.com/sched-ext/scx/issues/3750). Это отчёт о конкретной конфигурации, не доказательство проблемы всех Deck. При выборе LAVD Bosun устанавливает отсутствующий провайдер из настроенных SteamOS repositories и проверяет attach; при ошибке возвращает предыдущие настройки. Установка пакета не доказывает преимущество scheduler.
* **Governor powersave имеет разный смысл:** active amd-pstate-epp позволяет динамическое управление, legacy acpi-cpufreq powersave может фиксировать низкую частоту. Поэтому встроенный профиль с EPP не переводит legacy driver в powersave, если EPP недоступен.
* **Split-lock tweaks не перенесены:** это не универсальное средство для AMD Zen 2. Наличие рецепта для Intel не означает применимость к обоим Deck; поддержка требует соответствующих CPU features.
* **Undervolt/BIOS/фиксированный GPU OD range:** потенциально интересны, но сохранение скрытого текущего OD state и восстановление firmware нельзя подменять guessed defaults. Эти функции не заявляются; графические пакеты и ядро также не заменяются без отдельного доказанного сценария.
* **FPS cap/refresh остаются штатными Steam:** Decky даёт runtime-профиль мощности и системные параметры, но не выдаёт снижение графики/разрешения за оптимизацию и не навязывает частоту панели.

## Источники механизма

* [Linux VM sysctl](https://docs.kernel.org/admin-guide/sysctl/vm.html): compaction_proactiveness, стоимость compaction и reclaim.
* [Linux block queue sysfs](https://docs.kernel.org/block/queue-sysfs.html): read_ahead_kb.
* [zram-generator configuration](https://github.com/systemd/zram-generator/blob/main/man/zram-generator.conf.md): drop-in precedence, размеры и алгоритмы.
* [Linux workqueue.c](https://github.com/torvalds/linux/blob/master/kernel/workqueue.c): read-only module parameter `power_efficient` и применение флага в alloc_workqueue. Поэтому настройка загрузочная, не псевдо-runtime echo.
* [Linux workqueue.h](https://github.com/torvalds/linux/blob/master/include/linux/workqueue.h): смысл WQ_POWER_EFFICIENT.
* [AMD P-state](https://docs.kernel.org/admin-guide/pm/amd-pstate.html): режимы и EPP.
* [Bus lock handling](https://www.kernel.org/doc/html/latest/arch/x86/buslock.html): аппаратные условия применимости split-lock handling.
* [Official Decky template](https://github.com/SteamDeckHomebrew/decky-plugin-template), [Decky SDK](https://github.com/SteamDeckHomebrew/decky-frontend-lib), [Decky installer](https://github.com/SteamDeckHomebrew/decky-installer/blob/main/cli/install_release.sh): API и устройство установки.

## CPUFreq ceiling и аппаратная частота

`cpu_max_mhz` задаёт верхнюю границу CPUFreq policy для всех обнаруженных policies. Нижняя граница временно уменьшается только если она выше запрошенного потолка; исходная пара сохраняется и восстанавливается с проверкой внешних изменений. Аппаратный диапазон читается из `cpuinfo_min_freq`/`cpuinfo_max_freq`; несовместимые значения пропускаются до записи.

Это не обещание аппаратного hard cap на любой SteamOS/BIOS. [CPUFreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html) различает policy limits, запрос драйвера и доступные показания аппаратной частоты; [AMD P-state](https://docs.kernel.org/admin-guide/pm/amd-pstate.html) переводит policy в CPPC performance requests. При превышении в оверлее требуется наблюдение под нагрузкой с известным драйвером, Boost, min/max и другими менеджерами питания. Короткая кнопка проверки читает эти данные; она не создаёт нагрузку и не доказывает отсутствие превышений за пределами выборки.

Альтернативный путь [PowerTools](https://github.com/NGnius/PowerTools/blob/main/backend/src/settings/steam_deck/cpu.rs) использует `pp_od_clk_voltage` (`p core index clock`) в общем AMDGPU manual mode. В [исходниках Van Gogh](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/pm/swsmu/smu11/vangogh_ppt.c) commit применяет также GPU limits, а CPU значения являются soft min/max. Этот интерфейс не подставляется автоматически вместо CPUFreq без проверки реального устройства и исходных общих CPU/GPU ограничений: откат к зашитым 400/3500 MHz нарушил бы сохранённый baseline. Замена пути и аппаратное устранение сообщённого превышения пока не заявляются.
