#!/usr/bin/env python3
"""Colabの再構成結果を、信頼済み入力と保存重みに対して照合する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

import numpy as np
import torch


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(result, trusted, output_name='outputs'):
    expected = json.loads((trusted / 'SHA256.json').read_text())
    for name, value in expected.items():
        if not (trusted / name).resolve().is_relative_to(trusted.resolve()):
            raise ValueError('信頼済み入力のパスが不正です')
        if sha(trusted / name) != value or sha(result / 'input' / name) != value:
            raise ValueError('実行した入力が信頼済み入力と一致しません: ' + name)
    sys.path.insert(0, str(trusted.resolve()))
    import train_decoder as training
    output = result / output_name
    if not output.resolve().is_relative_to(result.resolve()):
        raise ValueError('結果フォルダーの指定が不正です')
    run = json.loads((output / 'run.json').read_text())
    complete = json.loads((output / 'completed.json').read_text())
    metrics = [json.loads(line) for line in (output / 'metrics.jsonl').read_text().splitlines()]
    if run['source_code_sha256'] != sha(trusted / 'train_decoder.py'):
        raise ValueError('実行コードの記録が一致しません')
    if 'band_loss_code_sha256' in run and run['band_loss_code_sha256'] != sha(trusted / 'band_loss.py'):
        raise ValueError('帯域損失コードの記録が一致しません')
    if run['baseline'] != 'mf07' or run['test_used_for_checkpoint_selection']:
        raise ValueError('教師または評価条件が異なります')
    if len({row['step'] for row in metrics}) != len(metrics):
        raise ValueError('学習ステップが重複しています')
    best_metric = min(metrics, key=lambda x: x['score'])
    best = torch.load(output / 'best.pt', map_location='cpu', weights_only=True)
    if best['step'] != complete['best_step'] or best['step'] != best_metric['step']:
        raise ValueError('最良重みの選択と保存が一致しません')
    if abs(best['validation_loss'] - best_metric['score']) > 1e-8:
        raise ValueError('評価値の記録が一致しません')
    if not all(torch.isfinite(tensor).all() for tensor in best['model'].values()):
        raise ValueError('重みに非有限値があります')
    torch.set_num_threads(2)
    data = training.load_data(trusted / 'data')
    expected_ids = {key: [x['row']['sample_id'] for x in items] for key, items in data.items()}
    if run['split_ids'] != expected_ids:
        raise ValueError('実行時の文章分割が一致しません')
    if {x['id'] for x in complete['samples']} != set(expected_ids['test']):
        raise ValueError('最終比較音声が揃っていません')
    model = training.Reconstruction(*training.normalization(data['train']))
    model.load_state_dict(best['model'], strict=True)
    model.eval()
    samples = []
    with torch.no_grad():
        for item in data['test']:
            sample_id = item['row']['sample_id']
            folder = output / sample_id
            raw = np.load(folder / 'reconstruction_float32.npy', allow_pickle=False)
            if raw.shape != (item['samples'],) or not np.isfinite(raw).all():
                raise ValueError('比較音声の長さまたは値が不正です')
            regenerated = model(item['z'][None])[0, :item['samples']].numpy()
            delta = regenerated - raw
            max_error = float(np.max(np.abs(delta)))
            # CUDAとCPUの演算差は許容し、違う重みや音声の混入は拒否する。
            if max_error > .005:
                raise ValueError(f'保存重みからの音声再現が一致しません: {sample_id} {max_error}')
            record = next(x for x in complete['samples'] if x['id'] == sample_id)
            gain = record['shared_preview_gain']
            if not 0 < gain <= 1:
                raise ValueError('比較用ゲインが不正です')
            wav_checks = {}
            for name, source in [('reference', item['y'][:item['samples']].numpy()), ('reconstruction', raw)]:
                with wave.open(str(folder / (name + '.wav'))) as wav:
                    if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (22050, 1, 2):
                        raise ValueError('比較用WAVの形式が不正です')
                    pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
                expected_pcm = np.rint(source * gain * 32767).astype('<i2')
                if pcm.shape != expected_pcm.shape:
                    raise ValueError('比較用WAVの長さが一致しません')
                pcm_delta = pcm.astype(np.int32) - expected_pcm.astype(np.int32)
                pcm_error = int(np.max(np.abs(pcm_delta)))
                # 教師のリサンプリングはCPU間の浮動小数演算差により丸めが1LSB違い得る。
                # 保存済みfloat32から書いた再構成WAVには完全一致を要求する。
                if pcm_error > (1 if name == 'reference' else 0):
                    raise ValueError('比較用WAVが記録された元音声・ゲインと一致しません')
                wav_checks[name] = {'max_pcm16_error': pcm_error,
                                    'different_samples': int(np.count_nonzero(pcm_delta))}
            samples.append({'id': sample_id, 'seconds': len(raw) / 22050,
                            'cpu_cuda_max_abs_error': max_error,
                            'cpu_cuda_rms_error': float(np.sqrt(np.mean(delta ** 2))),
                            'raw_peak': float(np.max(np.abs(raw))),
                            'raw_rms': float(np.sqrt(np.mean(raw ** 2))),
                            'shared_preview_gain': gain, 'wav_checks': wav_checks})
    return {'integrity_passed': True, 'perceptual_quality_verified': False,
            'baseline': run['baseline'], 'objective': run.get('objective', 'baseline'), 'device': run['device'],
            'last_logged_step': max(x['step'] for x in metrics), 'best_step': best['step'],
            'initial_validation_loss': metrics[0]['score'],
            'best_validation_loss': best['validation_loss'],
            'best_checkpoint_sha256': sha(output / 'best.pt'),
            'input_files_verified': len(expected), 'samples': samples}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--trusted-input', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output-name', default='outputs')
    args = parser.parse_args()
    report = audit(args.result, args.trusted_input, args.output_name)
    with args.report.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False))
