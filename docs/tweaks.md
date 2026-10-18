# Оптимизации Bosun и технические основания

В 0.3 отбор расширен на весь Linux/Steam Deck стек, а не только SDWEAK. [Полная таблица решений и сравнение с SDWEAK](global-analysis.md), [76 системных ключей и процессные параметры](catalog.md), [различия LCD/OLED](hardware-and-tweaks.md).

Recommended реально применяет: программные mitigations off (boot), NMI watchdog0, page-cluster0 при приоритетном zram, MGLRU full с сохранением старших bits, THP defrag defer+madvise, /home noatime при отдельном поддерживаемом mount и zswap off при исключительно zram swap. Совпадения с текущим состоянием показываются как UNCHANGED, отсутствующие интерфейсы — SKIP; число ключей не равно числу фактических изменений.

Профили игр содержат временные системные CPU/EPP/Boost/TDP/GPU, VM или I/O настройки. Launch presets отдельно задают окружение и unprivileged wrappers конкретного процесса через настоящую строку Steam. Boot, SMT, LAVD, zram size/compressor, mount и memlock остаются глобальными; игровая сессия не перезагружает драйверы/ядро и не делает swapoff.

Обязательны capability checks текущего DMI/SteamOS/kernel/driver, проверка выбранного значения, сохранение исходного состояния, journal до записи, идемпотентность, проверка результата и compare-and-swap rollback. RO→RW выполняется автоматически на необходимое время, затем возвращается исходный режим. Package install не меняет версии уже установленных dependencies и не удаляет программы. Thermal/hardware protections сохраняются.

Документация и исходники подтверждают механизм. Они **не подтверждают измеренный прирост** на конкретной игре/Deck. FPS, 1% low, frametime и battery Wh проверяются вместе при неизменном качестве, cap и сцене по [A/B-протоколу](testing.md). Физического Deck в среде разработки нет.
