#!/usr/bin/env python3
"""保存モデルをCPUで再実行し、入力・選択基準・予測・試聴音声を照合する。"""
import argparse
import json
from pathlib import Path
import time
import wave

import numpy as np
import torch
from train_world import load_data, normalization, load_model, evaluate, digest, write_json, STAT_NAMES
from world_vocoder import synthesize, SR
from compare_band_results import save_previews


def pcm(path, expected_sha):
    if digest(path) != expected_sha:
        raise ValueError('試聴基準のSHAが異なります')
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2):
            raise ValueError('試聴基準の形式が異なります')
        return np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768


def run(args):
    torch.set_num_threads(2)
    manifest = json.loads((args.trusted / 'SHA256.json').read_text())
    for name, expected in manifest.items():
        if digest(args.trusted / name) != expected:
            raise ValueError('保存済み入力が一致しません')
    for p in (args.result / 'input').iterdir():
        if p.is_file() and p.read_bytes() != (args.trusted / p.name).read_bytes():
            raise ValueError('Colabで入力が変更されています')
    run_info = json.loads((args.result / 'output/run.json').read_text())
    completed = json.loads((args.result / 'output/completed.json').read_text())
    for name, expected in run_info['code_sha256'].items():
        if digest(args.trusted / name) != expected or digest(Path(__file__).with_name(name)) != expected:
            raise ValueError('学習・推論コードが一致しません')
    data = load_data(args.trusted / 'data')
    if run_info['split_ids'] != {k: [x['id'] for x in v] for k, v in data.items()}:
        raise ValueError('Colabの分割が一致しません')
    model, state = load_model(args.result / 'output/best.pt')
    normalization_differences = {}
    for name, expected in zip(STAT_NAMES, normalization(data['train'])):
        actual = getattr(model, name)
        # ARM/x86のFP32並列加算では末尾ビットが異なるため、絶対・相対許容差を併用する。
        torch.testing.assert_close(actual, expected, atol=1e-6, rtol=1e-6)
        normalization_differences[name] = float((actual - expected).abs().max())
    logs = [json.loads(s) for s in (args.result / 'output/metrics.jsonl').read_text().splitlines()]
    scores = [(r['score'], r['step']) for r in logs] + [(completed['endpoint_validation_loss'], completed['updates'])]
    score, step = min(scores)
    if state['step'] != step or completed['best_step'] != step or abs(state['score'] - score) > 1e-8:
        raise ValueError('検証結果による最良モデル選択が一致しません')
    validation_score, _ = evaluate(model, data['validation'])
    test_score, test_rows = evaluate(model, data['test'])
    np.testing.assert_allclose([validation_score, test_score],
                               [completed['best_validation_loss'], completed['test_loss']], atol=1e-5, rtol=1e-5)
    analysis = args.result / 'analysis'
    analysis.mkdir(exist_ok=False)
    rows, reply = [], None
    for item in data['test']:
        started = time.monotonic()
        with torch.no_grad():
            features, f0, uv = model.physical(item['z'][None])
        arrays = {k: v[0].numpy() for k, v in {'features': features, 'f0': f0, 'uv_probability': uv}.items()}
        differences = {}
        with np.load(args.result / 'output/predictions' / (item['id'] + '.npz'), allow_pickle=False) as saved:
            if set(saved.files) != set(arrays):
                raise ValueError('予測項目が一致しません')
            for k, a in arrays.items():
                np.testing.assert_allclose(a, saved[k], atol=1e-4, rtol=1e-4)
                differences[k] = float(np.max(np.abs(a - saved[k])))
        y = synthesize(arrays['features'], arrays['f0'], item['samples'])
        seconds = time.monotonic() - started
        if not np.isfinite(y).all() or not np.any(y):
            raise ValueError('再生成した波形が不正です')
        np.save(analysis / (item['id'] + '.npy'), y, allow_pickle=False)
        rows.append({'id': item['id'], 'cpu_gpu_max_abs_difference': differences,
                     'raw_peak': float(np.max(np.abs(y))), 'seconds': seconds,
                     'duration': len(y) / SR})
        if item['id'] == 'mf07-test_reply': reply = y
    approved = pcm(args.reference / 'world_compact_parameters.wav',
                   '3bd909da60c80be8a36b4ea13e6784c7574cc893088e62e9aaaf7bfde8b63889')
    original = pcm(args.reference / 'accepted_reference_B.wav',
                   '5bcfe7d28d56dce7f014ab0f822864f3ef67e1024ceff282b1ee031b92452b89')
    if reply is None or reply.shape != approved.shape or reply.shape != original.shape:
        raise ValueError('同一文章の長さが一致しません')
    save_previews(analysis / 'reply', {'approved_world_compact': approved, 'predicted_world': reply,
                                     'original_irodori_B': original}, {'predicted': step}, pcm_scale=32768)
    stats = {}
    for name in ('approved_world_compact', 'predicted_world', 'original_irodori_B'):
        path = analysis / 'reply' / (name + '.wav')
        y = pcm(path, digest(path))
        p = np.rint(y * 32768).astype(np.int32)
        stats[name] = {'sha256': digest(path), 'samples': len(y),
                       'rms': float(np.sqrt(np.mean(y.astype(float) ** 2))),
                       'peak': float(np.max(np.abs(y))),
                       'rails': int(np.count_nonzero((p == 32767) | (p == -32768)))}
        if len(y) != 282240 or stats[name]['rails']:
            raise ValueError('試聴WAVが不正です')
    if np.ptp([s['rms'] for s in stats.values()]) > 5e-7:
        raise ValueError('試聴音声のRMSが一致しません')
    report = {'integrity_passed': True, 'input_files_verified': len(manifest), 'best_step': step,
              'checkpoint_sha256': digest(args.result / 'output/best.pt'),
              'normalization_cpu_max_abs_difference': normalization_differences,
              'cpu_validation_loss': validation_score, 'cpu_test_loss': test_score, 'test_metrics': test_rows,
              'reproduced_clips': rows, 'wav_validation': stats,
              'inference_uses_teacher_parameters': False, 'inference_uses_teacher_codec_latent': True,
              'quality_approved': False, 'text_to_speech_ready': False, 'C152_real_time_verified': False}
    write_json(analysis / 'audit.json', report)
    print(json.dumps({'integrity_passed': True, 'best_step': step, 'validation': validation_score,
                      'test': test_score, 'clips': len(rows), 'wav_validation': stats}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--result', type=Path, required=True)
    p.add_argument('--trusted', type=Path, required=True)
    p.add_argument('--reference', type=Path, required=True)
    run(p.parse_args())
