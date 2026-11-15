#!/usr/bin/env python3
"""固定したPyTorchモデルとC++推論の全文章・分割・波形を照合する。"""
import argparse
import json
import platform
from pathlib import Path
import subprocess

import numpy as np
import torch

from train_world import load_data, digest, write_json
from world_envelope_export import restore
from world_vocoder import synthesize
from prepare_world_envelope_baseline import CHECKPOINT_SHA


def execute(executable, weights, path, output, chunk, repeats):
    result = subprocess.run([str(executable.resolve()), str(weights.resolve()), str(path.resolve()),
                             str(output.resolve()), str(chunk), str(repeats)],
                            check=True, capture_output=True, text=True, timeout=120)
    stats = json.loads(result.stdout)
    if result.stderr:
        raise ValueError('ネイティブ実行が診断メッセージを返しました: ' + result.stderr)
    values = np.fromfile(output, dtype='<f4').reshape(64, stats['frames'])
    physical = np.fromfile(str(output) + '.physical.f32', dtype='<f4').reshape(values.shape)
    if not np.isfinite(values).all() or not np.isfinite(physical).all():
        raise ValueError('ネイティブ出力が非有限です')
    return values, physical, stats


def compare(output, physical, expected, physical_expected):
    np.testing.assert_allclose(output, expected, atol=3e-5, rtol=3e-5)
    np.testing.assert_allclose(physical[:62], physical_expected[:62], atol=2e-4, rtol=2e-5)
    np.testing.assert_allclose(physical[63], physical_expected[63], atol=5e-6, rtol=0)
    np.testing.assert_array_equal(physical[62] > 0, physical_expected[62] > 0)
    voiced = physical_expected[62] > 0
    cents = 1200 * np.log2(physical[62, voiced].astype(float) / physical_expected[62, voiced])
    pitch_error = float(np.abs(cents).max()) if len(cents) else 0.
    if pitch_error > .02:
        raise ValueError('音程の数値差が許容値を超えています')
    return {'normalized_max_abs_difference': float(np.abs(output - expected).max()),
            'feature_max_abs_difference': float(np.abs(physical[:62] - physical_expected[:62]).max()),
            'voicing_probability_max_abs_difference': float(np.abs(physical[63] - physical_expected[63]).max()),
            'voicing_labels_identical': True, 'pitch_max_abs_difference_cents': pitch_error}


@torch.no_grad()
def run(args):
    torch.set_num_threads(2)
    if digest(args.baseline / 'baseline.pt') != CHECKPOINT_SHA:
        raise ValueError('ユーザーが改善を確認した基準モデルではありません')
    model, metadata = restore(args.baseline / 'fp32')
    baseline = json.loads((args.baseline / 'baseline.json').read_text())
    if metadata != baseline['export']:
        raise ValueError('照合済みの書き出しと異なります')
    args.output.mkdir(parents=True, exist_ok=False)
    weights = args.baseline / 'fp32/weights.f32'
    rows = []
    for item in load_data(args.data)['test']:
        z = item['z'][None]
        expected = model(z)[0].numpy()
        features, f0, uv = model.physical(z)
        physical_expected = torch.cat((features, f0, uv), dim=1)[0].numpy()
        path = args.output / (item['id'] + '.input.f32')
        item['z'].numpy().astype('<f4').tofile(path)
        native = {}
        for mode, chunk in (('full', 0), ('chunks64', 64)):
            destination = args.output / (item['id'] + '.' + mode + '.f32')
            output, physical, timing = execute(args.executable, weights, path, destination, chunk, 3)
            parity = compare(output, physical, expected, physical_expected)
            timing['mac_neural_real_time_factor'] = timing['fastest_seconds'] / (item['samples'] / 22050)
            native[mode] = {'parity': parity, 'timing': timing,
                            'output_sha256': digest(destination),
                            'physical_sha256': digest(str(destination) + '.physical.f32')}
            if mode == 'full':
                full = output
            else:
                np.testing.assert_allclose(output, full, atol=3e-5, rtol=3e-5)
                native['native_full_vs_chunks_max_abs_difference'] = float(np.abs(output - full).max())
                # WORLD合成はここでは従来のPython版を使用する。
                reference = synthesize(physical_expected[:62], physical_expected[62], item['samples'])
                actual = synthesize(physical[:62], physical[62], item['samples'])
                np.save(args.output / (item['id'] + '.native_wave.npy'), actual, allow_pickle=False)
                relative = float(np.linalg.norm((actual - reference).astype(float)) /
                                 max(np.linalg.norm(reference.astype(float)), 1e-12))
                native['python_world_waveform_relative_l2_difference'] = relative
                native['python_world_rms_relative_difference'] = float(
                    abs(np.sqrt(np.mean(actual.astype(float) ** 2)) /
                        np.sqrt(np.mean(reference.astype(float) ** 2)) - 1))
        rows.append({'id': item['id'], 'input_sha256': digest(path), **native})
        print('NATIVE_PARITY', item['id'], native['chunks64']['parity'], flush=True)
    # 短文・左右の境界・chunkの端数を、サニタイザー付き実行でも検査する。
    boundary_rows = []
    rng = np.random.default_rng(20261113)
    for frames in (1, 49, 51, 65, 129):
        z = (torch.from_numpy(rng.normal(size=(1, 32, frames)).astype(np.float32)) * model.z_std + model.z_mean)
        expected = model(z)[0].numpy()
        f, p, u = model.physical(z)
        physical_expected = torch.cat((f, p, u), dim=1)[0].numpy()
        path = args.output / f'boundary_{frames}.input.f32'
        z[0].numpy().astype('<f4').tofile(path)
        output, physical, _ = execute(args.sanitized_executable, weights, path,
                                      args.output / f'boundary_{frames}.f32', 7, 1)
        boundary_rows.append({'frames': frames, 'chunk_frames': 7,
                              **compare(output, physical, expected, physical_expected)})
    test = subprocess.run([str(args.invariant_test.resolve())], check=True, capture_output=True, text=True, timeout=60)
    if test.stderr or test.stdout.strip() != 'Native invariant checks passed':
        raise ValueError('ネイティブ境界検査が完了していません')
    report = {
        'checkpoint_sha256': CHECKPOINT_SHA, 'weights_sha256': digest(weights),
        'rows': rows, 'sanitized_boundary_rows': boundary_rows,
        'native_invariant_checks_passed': True,
        'tolerances': {'normalized_atol': 3e-5, 'normalized_rtol': 3e-5,
                       'physical_features_atol': 2e-4, 'physical_features_rtol': 2e-5,
                       'voicing_probability_atol': 5e-6, 'pitch_max_cents': .02},
        'compiler': subprocess.run(['c++', '--version'], check=True, capture_output=True, text=True).stdout.splitlines()[0],
        'optimized_flags': '-std=c++17 -O2 -ffp-contract=off -Wall -Wextra -Werror',
        'sanitized_flags': '-std=c++17 -O1 -g -ffp-contract=off -fsanitize=address,undefined -fno-omit-frame-pointer -Wall -Wextra -Werror',
        'host_system': platform.system(), 'host_architecture': platform.machine(),
        'executable_sha256': digest(args.executable),
        'sanitized_executable_sha256': digest(args.sanitized_executable),
        'source_code_sha256': {str(p.relative_to(Path(__file__).parent)): digest(p) for p in
            [Path(__file__), *sorted(Path(__file__).with_name('native').glob('*.cpp')),
             *sorted(Path(__file__).with_name('native').glob('*.h'))]},
        'new_training_performed': False, 'weights_changed': False, 'firmware_changed': False,
        'text_to_speech_ready': False, 'C152_real_time_verified': False,
        'limitations': [
            'C++へ移したのはニューラル予測器とパラメータ変換のみです。WORLD合成はPython版で照合しています。',
            '入力は教師由来のcodec潜在表現です。文章からの発音時間・内部表現の生成は未完成です。',
            '時間はMac上の3回中最速値で、C152の速度や消費電力を示しません。',
            'メモリー数値は予測器の重み、作業領域、窓の入出力です。WORLD、文章処理、音声バッファ、OSは含みません。',
            '音程がごくわずかに異なると再合成波形の位相も変わります。波形の完全一致や聴感合格を主張しません。',
            '語尾「かな」に残る再合成の違和感は未解決です。',
        ],
    }
    write_json(args.output / 'report.json', report)
    print(json.dumps({'clips': len(rows), 'boundary_cases': len(boundary_rows),
                      'maximum_normalized_difference': max(r['chunks64']['parity']['normalized_max_abs_difference'] for r in rows),
                      'maximum_pitch_difference_cents': max(r['chunks64']['parity']['pitch_max_abs_difference_cents'] for r in rows),
                      'maximum_waveform_relative_l2': max(r['python_world_waveform_relative_l2_difference'] for r in rows)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'data', 'executable', 'sanitized-executable', 'invariant-test', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
