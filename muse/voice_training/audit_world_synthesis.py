#!/usr/bin/env python3
"""C++ WORLDの全文・分割と従来Python波形を、同じ物理特徴10文で照合する。"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess

import numpy as np

from prepare_world_source import prepare
from world_vocoder import synthesize


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    source = prepare()
    previous = json.loads((args.predictor / 'report.json').read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for item in previous['rows']:
        key = item['id']
        path = args.predictor / (key + '.chunks64.f32.physical.f32')
        if digest(path) != item['chunks64']['physical_sha256']:
            raise ValueError('Audited predictor output changed: ' + key)
        physical = np.fromfile(path, dtype='<f4').reshape(64, -1)
        prior_wave = np.load(args.predictor / (key + '.native_wave.npy'), allow_pickle=False)
        expected = synthesize(physical[:62], physical[62], len(prior_wave))
        np.testing.assert_array_equal(expected, prior_wave)
        stats = {}
        for mode, block in (('reference', 0), ('stream512', 512), ('stream7', 7), ('stream1024', 1024)):
            output = args.output / (key + '.' + mode + '.f32')
            result = subprocess.run([str(args.executable.resolve()), str(path.resolve()), str(output.resolve()),
                                     str(len(expected)), str(block), '3'], check=True, capture_output=True, text=True, timeout=120)
            if result.stderr:
                raise ValueError(result.stderr)
            timing = json.loads(result.stdout)
            actual = np.fromfile(output, dtype='<f4')
            if actual.shape != expected.shape or not np.isfinite(actual).all():
                raise ValueError('Invalid waveform')
            if not block:
                full = actual
            else:
                np.testing.assert_array_equal(actual, full)
            diff = actual.astype(float) - expected.astype(float)
            relative = float(np.linalg.norm(diff) / max(np.linalg.norm(expected.astype(float)), 1e-12))
            if relative > 1e-6 or np.max(np.abs(diff)) > 1e-6:
                raise ValueError('Python parity outside tolerance')
            stats[mode] = {**timing, 'relative_l2_difference_from_python': relative,
                           'max_abs_difference_from_python': float(np.max(np.abs(diff))),
                           'mac_real_time_factor': timing['fastest_seconds'] / (len(expected) / 22050),
                           'wave_sha256': digest(output)}
        rows.append({'id': key, 'physical_sha256': digest(path), **stats})
        checked = args.output / (key + '.sanitized.f32')
        sanitized = subprocess.run([str(args.sanitized_executable.resolve()), str(path.resolve()), str(checked.resolve()),
                                    str(len(expected)), '512', '1'], check=True, capture_output=True, text=True, timeout=120)
        if sanitized.stderr:
            raise ValueError(sanitized.stderr)
        np.testing.assert_array_equal(np.fromfile(checked, dtype='<f4'), full)
        print('WORLD_PARITY', key, stats['stream512']['relative_l2_difference_from_python'], flush=True)
    invariant = subprocess.run([str(args.test.resolve())], check=True, capture_output=True, text=True, timeout=120)
    if invariant.stderr or invariant.stdout.strip() != 'WORLD stream invariant checks passed':
        raise ValueError('Sanitized invariant checks did not pass')
    here = Path(__file__).parent
    report = {
        'rows': rows,
        'predictor_report_sha256': digest(args.predictor / 'report.json'),
        'world_source_manifest_sha256': digest(here / 'world_source_manifest.json'),
        'world_revision': source.name.removeprefix('world-'),
        'checkpoint_sha256': previous['checkpoint_sha256'], 'weights_sha256': previous['weights_sha256'],
        'full_and_stream_bit_identical': True,
        'sanitized_boundary_cases': 240,
        'sanitized_invariant_test_passed': True,
        'sanitized_real_utterances': len(rows),
        'host_system': platform.system(), 'host_architecture': platform.machine(),
        'executable_sha256': digest(args.executable), 'sanitized_test_sha256': digest(args.test),
        'sanitized_executable_sha256': digest(args.sanitized_executable),
        'source_sha256': {str(p.relative_to(here)): digest(p) for p in
            [Path(__file__), here / 'prepare_world_source.py', here / 'build_world_native.py',
             *sorted((here / 'native').glob('world_s*.cpp')), here / 'native/world_stream.h',
             here / 'native/test_world_stream.cpp']},
        'new_training_performed': False, 'weights_changed': False, 'firmware_changed': False,
        'C152_real_time_verified': False, 'text_to_speech_ready': False,
        'limitations': [
            'Mac上の計測。C152の実行速度・ヒープピーク・割り込み耐性は未測定。',
            'ラッパー状態量はWORLD内部のFFT・復号器の動的確保、入力特徴、予測器、OSを含まない。',
            '波形メモリーは一定だが、呼び出し元は現時点で全文の64次元物理特徴を保持する。',
            '入力は教師codec潜在表現。文章から内部表現・発音時間を作る部分は未完成。',
            '既存の声質と数値的に一致させた移植であり、「かな」の残る違和感を改善する変更ではない。',
        ]}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'clips': len(rows), 'max_relative_l2': max(r['stream512']['relative_l2_difference_from_python'] for r in rows),
                      'wrapper_bytes': max(r['stream512']['wrapper_state_bytes'] for r in rows)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('predictor', 'output', 'executable', 'sanitized-executable', 'test'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
