#!/usr/bin/env python3
"""接続済みC152の診断ビルドで固定文のローカル測定を実行する。"""
import argparse
import json
import os
from pathlib import Path
import re
import time

import serial


def playback_report(lines):
    for line in lines:
        match = re.search(r'speaker played=(\d) first_write=([-\d.]+)s underrun_blocks=(\d+) clipped=(\d+) samples=(\d+) duration=([\d.]+)s', line)
        if match:
            played, first, underruns, clipped, samples, duration = match.groups()
            return {'audio_played': played == '1', 'speaker': {
                'first_write_seconds': float(first), 'underrun_blocks': int(underruns),
                'clipped_samples': int(clipped), 'written_samples': int(samples),
                'elapsed_seconds': float(duration), 'dac_latency_measured': False,
                'continuous': played == '1' and int(underruns) == 0,
                'start_within_five_seconds': played == '1' and 0 <= float(first) <= 5.0}}
    return {'audio_played': False}


def run(args, port=None):
    delays = getattr(args, 'start_delays_ms', None)
    if delays and (len(delays) != args.repeats or any(not 1000 <= delay <= 5000 for delay in delays)):
        raise ValueError('Specify one 1000–5000 ms start delay per repetition')
    if args.output.exists():
        raise ValueError('Output already exists')
    args.output.mkdir(parents=True, mode=0o700)
    raw = os.fdopen(os.open(args.output / 'serial.log', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb')
    owns_port = port is None
    if owns_port:
        port = serial.Serial(None, 115200, timeout=.2)
        port.dtr = port.rts = False
        port.port = args.port
        port.open()
    else:
        # A flasher can hand over its live serial object without reopening USB.
        if not port.is_open:
            raise ValueError('Borrowed USB port is closed')
        port.baudrate = 115200
        port.timeout = .2
    sent = started = False
    result = memory = None
    # Let boot-time pairing/network allocation settle before taking a heap
    # minimum. This wait is local to the diagnostic runner, not a UI freeze.
    next_probe = time.monotonic() + 20
    readiness_deadline = time.monotonic() + 60
    job_deadline = None
    deadline = time.monotonic() + 30 + 150 * args.repeats
    lines = []
    measurements = []
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not sent and now >= readiness_deadline:
                raise RuntimeError('Console not ready within 60 seconds; inspect private serial.log')
            if sent and job_deadline is not None and now >= job_deadline:
                raise RuntimeError('Diagnostic did not finish within 130 seconds; inspect private serial.log')
            if not sent and now >= next_probe:
                port.write(b'\n>sumi.power\n')
                next_probe = now + 3
            data = port.readline()
            if not data:
                continue
            raw.write(data); raw.flush()
            line = re.sub(r'\x1b\[[0-9;]*m', '', data.decode('utf-8', 'replace')).strip()
            if '@sumi.power ' in line and not sent:
                # Console readiness is enough; the command itself checks that
                # no voice turn or unsent note is active before queueing work.
                suffix = f' {delays[len(measurements)]}' if delays else ''
                port.write(('>sumi.tts_bench' + suffix + '\n').encode('ascii')); sent = True
                job_deadline = now + 130
            if '@sumi.tts_bench busy' in line:
                sent = False; next_probe = now + 3
            if 'sumi_world_bench:' in line:
                print(line, flush=True); lines.append(line)
                if re.match(r'E \(', line):
                    raise RuntimeError('Device rejected diagnostic; inspect private serial.log')
                if ' start:' in line: started = True
                if ' result ' in line: result = line
                if ' memory ' in line: memory = line
            if 'Guru Meditation' in line or 'panic' in line.lower() or 'abort() was called' in line:
                raise RuntimeError('Device error; inspect private serial.log')
            if result and memory:
                ok = 'ok=1 ' in result and f'samples={args.samples} ' in result
                measurements.append({'completed': ok, 'result': result, 'memory': memory,
                                     'diagnostic_lines': lines, **playback_report(lines),
                                     'text_frontend_included': False, 'expected_wave_samples': args.samples})
                if delays:
                    measurements[-1]['requested_start_delay_ms'] = delays[len(measurements) - 1]
                if not ok or len(measurements) == args.repeats:
                    break
                # Keep this USB connection and boot instance for comparable
                # repeats; reopening the port may restart the whole device.
                sent = started = False
                result = memory = None
                lines = []
                next_probe = time.monotonic() + 3
                readiness_deadline = time.monotonic() + 60
                job_deadline = None
        ok = len(measurements) == args.repeats and all(row['completed'] for row in measurements)
        report = measurements[0] if args.repeats == 1 and measurements else {
            'completed': ok, 'same_boot': True, 'measurements': measurements,
            'expected_repeats': args.repeats,
            'audio_played': any(row['audio_played'] for row in measurements),
            'text_frontend_included': False}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        if not ok:
            raise RuntimeError('Diagnostic cancelled, timed out, or failed')
    finally:
        if owns_port:
            port.close()
        raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, choices=[16000, 22050, 79360, 109368, 148480], default=22050)
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=1)
    parser.add_argument('--start-delays-ms', type=int, nargs='+')
    run(parser.parse_args())
