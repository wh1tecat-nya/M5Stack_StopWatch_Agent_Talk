#!/usr/bin/env python3
"""C152のアプリだけを一時更新し、同じUSB接続で測定・復元する。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

APP_OFFSET = 0x20000
APP_BYTES = 0x800000
TABLE_OFFSET = 0x10000


def checked_bytes(path, expected, size=None):
    data = path.read_bytes()
    if (size is not None and len(data) != size) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('File size or SHA-256 mismatch: ' + str(path))
    return data


def observe_health(port, output):
    output.mkdir(mode=0o700)
    report = {'seconds': 45, 'console_responses': 0, 'wifi_connected': False,
              'muse_vm_connected': False, 'panic_seen': False}
    begin = time.monotonic()
    next_probe = begin + 10
    with os.fdopen(os.open(output / 'serial.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as raw:
        while time.monotonic() - begin < 45:
            if time.monotonic() >= next_probe:
                port.write(b'\n>sumi.power\n')
                next_probe = time.monotonic() + 5
            data = port.readline()
            if not data:
                continue
            raw.write(data)
            raw.flush()
            line = data.decode('utf-8', 'replace')
            report['console_responses'] += '@sumi.power ext5v=' in line
            report['wifi_connected'] |= 'got IP ' in line
            report['muse_vm_connected'] |= 'connected to VM ' in line
            report['panic_seen'] |= any(s in line for s in ('ESP_ERROR_CHECK failed', 'Guru Meditation', 'abort()'))
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


class Device:
    def __init__(self, name):
        import esptool
        self.api = esptool
        self.name = name
        self.port = None
        self.esp = None

    def connect(self):
        self.esp = self.api.detect_chip(self.port if self.port is not None else self.name,
                                        baud=115200, connect_attempts=3)
        self.port = self.esp._port
        if self.esp.CHIP_NAME != 'ESP32-S3':
            raise ValueError('Expected ESP32-S3')
        self.esp = self.esp.run_stub()
        self.port = self.esp._port
        self.esp.change_baud(921600)
        self.api.attach_flash(self.esp)

    def check_original(self, backup, table):
        if self.api.read_flash(self.esp, TABLE_OFFSET, len(table), no_progress=True) != table:
            raise ValueError('Device partition table differs from preserved table')
        self.api.verify_flash(self.esp, [(APP_OFFSET, backup)], diff=False)

    def write(self, data):
        self.api.write_flash(self.esp, [(APP_OFFSET, data)], compress=True)

    def boot(self):
        self.esp.hard_reset()
        # Keep the same serial object. Opening another handle can reset native
        # USB again between flashing and collecting the first application logs.
        self.port.baudrate = 115200
        self.port.timeout = .2

    def close(self):
        if self.port is not None:
            self.port.close()


def experiment(args, device=None, measure=None, health=observe_health):
    from run_world_benchmark import run
    from types import SimpleNamespace
    backup = checked_bytes(args.backup, args.backup_sha256, APP_BYTES)
    table = checked_bytes(args.table, args.table_sha256, 4096)
    candidate = checked_bytes(args.candidate, args.candidate_sha256)
    if not (1000000 < len(candidate) <= APP_BYTES) or candidate[0] != 0xe9:
        raise ValueError('Invalid candidate application image')
    # Verify the preserved table actually describes the only writable region.
    import struct
    app_entries = []
    for offset in range(0, len(table) - 31, 32):
        magic, kind, subtype, start, size = struct.unpack_from('<HBBII', table, offset)
        if magic == 0x50aa and kind == 0:
            app_entries.append((start, size))
    if app_entries != [(APP_OFFSET, APP_BYTES)]:
        raise ValueError('Expected one 8 MiB application at 0x20000')
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    device = device if device is not None else Device(args.port)
    measure = measure if measure is not None else run
    touched = False
    report = {'candidate_sha256': args.candidate_sha256, 'original_sha256': args.backup_sha256,
              'benchmark_completed': False, 'original_restored': False}
    try:
        device.connect()
        device.check_original(backup, table)
        touched = True  # Even a partially failed write requires restoration.
        device.write(candidate)
        device.boot()
        measure(SimpleNamespace(port=args.port, output=args.output / 'measurement',
                                samples=getattr(args, 'samples', 109368), repeats=args.repeats,
                                start_delays_ms=getattr(args, 'start_delays_ms', None)), device.port)
        report['benchmark_completed'] = True
    except (Exception, KeyboardInterrupt) as error:
        report['benchmark_error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if touched:
            try:
                device.connect()
                device.write(backup)
                report['original_restored'] = True
                device.boot()
                report['restored_health'] = health(device.port, args.output / 'restored')
            except (Exception, KeyboardInterrupt) as error:
                report['restoration_error'] = type(error).__name__ + ': ' + str(error)
        else:
            # A read-only preflight failure may have left the chip in download
            # mode. Attempt to resume its unchanged application, without writes.
            try:
                device.boot()
            except Exception:
                pass
        try:
            device.close()
        except Exception as error:
            report['close_error'] = str(error)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    for name in ('candidate', 'backup', 'table'):
        parser.add_argument('--' + name, type=Path, required=True)
        parser.add_argument('--' + name + '-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=3)
    parser.add_argument('--samples', type=int, choices=[79360, 109368, 148480], default=109368)
    parser.add_argument('--start-delays-ms', type=int, nargs='+')
    result = experiment(parser.parse_args())
    print(json.dumps(result, indent=2), flush=True)
    status = result.get('restored_health', {})
    healthy = status.get('console_responses', 0) > 0 and status.get('wifi_connected') and status.get('muse_vm_connected') and not status.get('panic_seen')
    raise SystemExit(0 if result['benchmark_completed'] and result['original_restored'] and healthy else 2)
