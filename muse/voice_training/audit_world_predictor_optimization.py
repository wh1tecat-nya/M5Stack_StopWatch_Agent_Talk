#!/usr/bin/env python3
"""最適化した予測器を固定済み10文へ照合する。音質の新規評価には代用しない。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    baseline = json.loads((args.baseline / 'report.json').read_text())
    if sha(args.weights) != baseline['weights_sha256']:
        raise ValueError('Baseline weights changed')
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for row in baseline['rows']:
        key = row['id']
        source = args.baseline / (key + '.input.f32')
        if sha(source) != row['input_sha256']:
            raise ValueError('Baseline input changed: ' + key)
        for chunk in args.chunks:
            out = args.output / (key + f'.chunk{chunk}.f32')
            result = subprocess.run([str(args.executable.resolve()), str(args.weights.resolve()),
                                     str(source.resolve()), str(out.resolve()), str(chunk), '1', 'tiled'],
                                    check=True, capture_output=True, text=True, timeout=120)
            if result.stderr:
                raise ValueError(result.stderr)
            for suffix, hash_key in [('', 'normalized_sha256'), ('.physical.f32', 'physical_sha256')]:
                actual = Path(str(out) + suffix)
                # Reports produced before this audit used output_sha256.
                expected_key = hash_key if hash_key in row['chunks64'] else 'output_sha256'
                if sha(actual) != row['chunks64'][expected_key]:
                    raise ValueError(f'Bitwise baseline mismatch: {key}, chunk={chunk}, {suffix}')
            rows.append({'id': key, 'chunk': chunk, 'measurement': json.loads(result.stdout)})
            print(key, chunk, 'bit-identical', flush=True)
    test = subprocess.run([str(args.test.resolve())], check=True, capture_output=True, text=True, timeout=120)
    if test.stderr:
        raise ValueError(test.stderr)
    report = {'rows': rows, 'bit_identical_to_baseline': True,
              'weights_sha256': sha(args.weights), 'executable_sha256': sha(args.executable),
              'baseline_report_sha256': sha(args.baseline / 'report.json'),
              'test_stdout': test.stdout.strip(), 'device_verified': False}
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ['baseline', 'weights', 'executable', 'test', 'output']:
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--chunks', nargs='+', type=int, default=[256, 512])
    run(parser.parse_args())
