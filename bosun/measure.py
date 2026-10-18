# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit-duration battery capture and frame-time analysis; no background daemon."""
import csv
import io
import json
import math
from pathlib import Path
import statistics
import time

from .core import BosunError, atomic


def battery_sample(system):
    batteries = [p for p in system.glob('/sys/class/power_supply/*') if system.read(p + '/type') == 'Battery']
    if len(batteries) != 1:
        raise BosunError('Exactly one battery required for total-device energy measurement')
    p = batteries[0]
    if system.read(p + '/status') != 'Discharging':
        raise BosunError('Battery must be discharging; unplug charger/dock power')
    energy = system.read(p + '/energy_now')
    charge, voltage = system.read(p + '/charge_now'), system.read(p + '/voltage_now')
    power = system.read(p + '/power_now')
    current = system.read(p + '/current_now')
    # power_supply units: microWh/microW, or microA * microV / 1e12 W.
    watts = abs(float(power)) / 1e6 if power is not None else (
        abs(float(current) * float(voltage)) / 1e12 if current is not None and voltage is not None else None)
    if energy is None and watts is None:
        raise BosunError('Battery exports neither energy_now nor discharge power/current')
    return {'monotonic_s': time.monotonic(), 'energy_wh': float(energy) / 1e6 if energy is not None else None,
            'battery_w': watts, 'charge_ah': float(charge) / 1e6 if charge else None,
            'voltage_v': float(voltage) / 1e6 if voltage else None}


def energy_metrics(samples):
    duration = samples[-1]['monotonic_s'] - samples[0]['monotonic_s']
    if duration <= 0:
        raise BosunError('Measurement duration must be positive')
    first, last = samples[0]['energy_wh'], samples[-1]['energy_wh']
    integral = None
    if all(s['battery_w'] is not None for s in samples):
        integral = sum((a['battery_w'] + b['battery_w']) / 2 * (b['monotonic_s'] - a['monotonic_s']) / 3600
                       for a, b in zip(samples, samples[1:]))
    delta = first - last if first is not None and last is not None else None
    if delta is not None and delta < 0:
        raise BosunError('Battery energy increased: invalid discharge run')
    # Gauges are quantised: preserve BOTH values; zero delta cannot prove zero consumption.
    chosen = delta if delta is not None and delta > 0 else integral
    return {'duration_s': duration, 'battery_energy_delta_wh': delta, 'integrated_battery_wh': integral,
            'average_total_device_w': chosen * 3600 / duration if chosen is not None else None,
            'energy_basis': 'energy_now delta' if delta is not None and delta > 0 else 'power/current integration',
            'note': 'Battery gauge quantisation and sensor error apply. This is total device discharge, not APU TDP.'}


def capture(system, engine, output, seconds, metadata):
    if not 10 <= seconds <= 7200:
        raise BosunError('Duration must be 10..7200 seconds')
    required = ['game', 'scene', 'graphics', 'fps_cap', 'refresh_hz', 'power_mode', 'brightness', 'proton']
    if any(not metadata.get(k) for k in required):
        raise BosunError('Metadata required: ' + ', '.join(required))
    target = Path(output)
    if target.exists():
        raise BosunError('Output already exists; choose a new run filename')
    start = time.monotonic()
    samples = [battery_sample(system)]
    report = engine.report()
    data = {'metadata': metadata, 'before': report, 'samples': samples, 'complete': False}
    atomic(target, json.dumps(data, indent=2))
    try:
        while time.monotonic() - start < seconds:
            time.sleep(min(1, max(0, seconds - (time.monotonic() - start))))
            sample = battery_sample(system)
            sample['memory_psi'] = system.read('/proc/pressure/memory')
            sample['zram_mm_stat'] = {p: system.read(p) for p in system.glob('/sys/block/zram*/mm_stat')}
            sample['apu_caps_uw'] = {p: system.read(p) for p in system.glob('/sys/class/hwmon/hwmon*/power[12]_cap')}
            samples.append(sample)
        data.update(metrics=energy_metrics(samples), after=engine.report(), complete=True)
    finally:
        atomic(target, json.dumps(data, indent=2, ensure_ascii=False))
    return data['metrics']


def frame_metrics(path):
    # MangoHud raw logs contain a metadata row/header BEFORE the per-sample CSV header.
    lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    header = next((i for i, line in enumerate(lines) if 'frametime' in next(csv.reader([line]))), None)
    if header is None:
        raise BosunError('MangoHud CSV needs frametime column (milliseconds)')
    rows = list(csv.DictReader(io.StringIO('\n'.join(lines[header:]))))
    values = []
    elapsed = []
    for row in rows:
        try:
            value = float(row['frametime'])
            if math.isfinite(value) and value > 0:
                values.append(value)
                if row.get('elapsed'):
                    elapsed.append(float(row['elapsed']))
        except (ValueError, KeyError):
            continue
    if len(values) < 100:
        raise BosunError('At least 100 valid frametime samples required')
    values.sort()
    def percentile(q):
        return values[min(len(values) - 1, math.ceil(q * len(values)) - 1)]
    count = max(1, math.ceil(len(values) * .01))
    return {'samples': len(values), 'fps_from_mean_frametime': 1000 / statistics.mean(values),
            'one_percent_low_fps': 1000 / statistics.mean(values[-count:]),
            'median_ms': statistics.median(values), 'p95_ms': percentile(.95), 'p99_ms': percentile(.99),
            'p999_ms': percentile(.999), 'spikes_over_50ms': sum(v > 50 for v in values),
            'warning': 'Valid frame metrics require per-frame raw log (MangoHud log_interval=0). Periodic samples cannot establish true 1% lows.',
            'elapsed_column_present': bool(elapsed)}
