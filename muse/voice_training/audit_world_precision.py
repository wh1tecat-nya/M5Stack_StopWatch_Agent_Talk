#!/usr/bin/env python3
"""固定包絡モデルの10文で、FP32波形の誤差・分割不変性・安全性を検証する。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    prior = json.loads((args.predictor / 'report.json').read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for item in prior['rows']:
        key = item['id']
        features = args.predictor / (key + '.chunks64.f32.physical.f32')
        if sha(features) != item['chunks64']['physical_sha256']:
            raise ValueError('Baseline changed: ' + key)
        expected = np.load(args.predictor / (key + '.native_wave.npy'), allow_pickle=False).astype(np.float64)
        reference = None
        timings = {}
        for name, exe, block in [('stream512', args.executable, 512), ('stream7', args.executable, 7),
                                  ('stream1024', args.executable, 1024), ('sanitized', args.sanitized_executable, 512)]:
            output = args.output / (key + '.' + name + '.f32')
            result = subprocess.run([str(exe.resolve()), str(features.resolve()), str(output.resolve()),
                                     str(expected.size), str(block), '1'], capture_output=True, text=True,
                                    check=True, timeout=120)
            if result.stderr:
                raise ValueError(result.stderr)
            actual = np.fromfile(output, dtype='<f4')
            if actual.shape != expected.shape or not np.isfinite(actual).all():
                raise ValueError('Invalid wave')
            if reference is None:
                reference = actual
            if name == 'sanitized':
                # Instrumented -O1 and production -O2 may select different
                # libm paths. Keep block-size comparisons bitwise, and bound
                # this compiler-only difference separately.
                delta = actual.astype(np.float64) - reference.astype(np.float64)
                if np.max(np.abs(delta)) > 5e-6 or np.linalg.norm(delta) / max(np.linalg.norm(reference.astype(np.float64)), 1e-12) > 1e-5:
                    raise ValueError('Sanitized build differs beyond rounding tolerance')
            else:
                np.testing.assert_array_equal(actual, reference)
            timings[name] = json.loads(result.stdout)
        diff = reference.astype(np.float64) - expected
        relative = float(np.linalg.norm(diff) / max(np.linalg.norm(expected), 1e-12))
        peak = float(np.max(np.abs(diff)))
        # About 80 dB waveform agreement; this is a numerical preservation gate,
        # not proof of perceptual equivalence or a substitute for an audition.
        if relative > 1e-4 or peak > 5e-4:
            raise ValueError(f'Precision gate failed: {key}, {relative}, {peak}')
        rows.append({'id': key, 'samples': expected.size, 'relative_l2': relative,
                     'max_abs': peak, 'features_sha256': sha(features), 'timings': timings})
        print(key, relative, peak, flush=True)
    test = subprocess.run([str(args.test.resolve())], check=True, capture_output=True, text=True, timeout=120)
    if test.stderr or test.stdout.strip() != 'WORLD stream invariant checks passed':
        raise ValueError('Boundary checks failed')
    report = {'rows': rows, 'baseline_weights_sha256': prior['weights_sha256'],
              'predictor_report_sha256': sha(args.predictor / 'report.json'),
              'max_relative_l2': max(row['relative_l2'] for row in rows),
              'max_absolute_error': max(row['max_abs'] for row in rows),
              'stream_blocks_bit_identical': True, 'sanitizer_agreement_max_abs_gate': 5e-6,
              'sanitizer_agreement_relative_l2_gate': 1e-5, 'boundary_cases': 240,
              'phase_accumulation': 'float64', 'wave_dsp': 'float32',
              'device_verified': False, 'audition_verified': False,
              'executables': {name: sha(getattr(args, name)) for name in ['executable', 'sanitized_executable', 'test']}}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['max_relative_l2', 'max_absolute_error']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ['predictor', 'output', 'executable', 'sanitized-executable', 'test']:
        parser.add_argument('--' + arg, type=Path, required=True)
    run(parser.parse_args())
