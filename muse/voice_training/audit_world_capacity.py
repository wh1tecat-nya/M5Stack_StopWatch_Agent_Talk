#!/usr/bin/env python3
"""容量を比較した追加学習を再現し、初期出力・保存点・試聴を照合する。"""
import argparse
import json
from pathlib import Path

import numpy as np
import torch
from train_world import load_data, load_model, digest, write_json, STAT_NAMES
from train_world_capacity import evaluate, BASE_SHA
from world_capacity_model import load as load_capacity, WIDTHS, HALO
from world_acoustic_loss import AcousticLoss
from world_vocoder import synthesize, SR, FRAME_MS
from audit_world_training import pcm
from compare_band_results import save_previews


def means(rows):
    return {k: float(np.mean([r[k] for r in rows if r[k] is not None]))
            for k in rows[0] if k != 'id'}


@torch.no_grad()
def windows(model, item, acoustic):
    prediction = model(item['z'][None])
    duration = item['samples'] / SR
    rows = []
    for second in range(int(np.ceil(duration))):
        start, stop = int(second * 1000 / FRAME_MS), min(int((second + 1) * 1000 / FRAME_MS), prediction.shape[-1])
        section = slice(start, stop)
        rows.append({'start_seconds': second, 'end_seconds': min(second + 1, duration),
                     **acoustic.metrics(prediction[:, :, section], item['features'][None, :, section],
                                        item['log_f0'][None, :, section], item['uv'][None, :, section], model)})
    return rows


def run(args):
    torch.set_num_threads(2)
    manifest = json.loads((args.trusted / 'SHA256.json').read_text())
    for name, expected in manifest.items():
        if digest(args.trusted / name) != expected:
            raise ValueError('保存済み入力が一致しません: ' + name)
    data = load_data(args.trusted / 'data')
    base, base_state = load_model(args.trusted / 'base.pt')
    if digest(args.trusted / 'base.pt') != BASE_SHA:
        raise ValueError('開始モデルが一致しません')
    acoustic = AcousticLoss()
    analysis = args.result / 'analysis'
    analysis.mkdir(exist_ok=False)
    report = {'input_files_verified': len(manifest), 'conditions': {}, 'base': {},
              'inference_uses_teacher_parameters': False, 'inference_uses_teacher_codec_latent': True,
              'quality_approved': False, 'text_to_speech_ready': False,
              'C152_real_time_verified': False, 'firmware_changed': False}
    for split in ('train', 'validation', 'test'):
        score, rows = evaluate(base, data[split], acoustic)
        report['base'][split] = {'score': score, 'means': means(rows), 'rows': rows}
    replies = {}
    reply_item = next(i for i in data['test'] if i['id'] == 'mf07-test_reply')
    report['base']['reply_per_second'] = windows(base, reply_item, acoustic)
    with torch.no_grad():
        features, f0, _ = base.physical(reply_item['z'][None])
    replies['previous_model'] = synthesize(features[0].numpy(), f0[0].numpy(), reply_item['samples'])
    previous_raw = np.load(args.previous / 'analysis/acoustic_mf07-test_reply.npy', allow_pickle=False)
    np.testing.assert_array_equal(replies['previous_model'], previous_raw)
    for condition in ('control', 'capacity'):
        folder = args.result / 'output' / condition
        info = json.loads((folder / 'run.json').read_text())
        done = json.loads((folder / 'completed.json').read_text())
        if info['condition'] != condition or info['base_sha256'] != BASE_SHA or info['base_step'] != base_state['step']:
            raise ValueError('条件または開始点が異なります')
        if (done['updates'], info['steps_requested'], info['seed'], info['batch'], info['learning_rate']) != (5000, 5000, 20261115, 8, .0002):
            raise ValueError('公平な比較に必要な更新数・条件が異なります')
        if info['split_ids'] != {k: [i['id'] for i in v] for k, v in data.items()}:
            raise ValueError('固定分割が変更されています')
        for name, sha in info['code_sha256'].items():
            if digest(args.trusted / name) != sha or digest(Path(__file__).with_name(name)) != sha:
                raise ValueError('学習コードが異なります')
        model, state = load_capacity(folder / 'best.pt')
        if state['condition'] != condition or info['width'] != WIDTHS[condition] or state['width'] != WIDTHS[condition] or info['halo_frames'] != HALO:
            raise ValueError('モデル幅が保存条件と異なります')
        endpoint, end_state = load_capacity(folder / 'endpoint.pt')
        initial, _ = load_capacity(args.trusted / 'base.pt', initial_condition=condition)
        initial_score, _ = evaluate(initial, data['validation'], acoustic)
        np.testing.assert_allclose(initial_score, done['initial_validation_score'], atol=1e-5, rtol=1e-5)
        initial_difference = 0.
        initial_uv_changes = 0
        with torch.no_grad():
            for items in data.values():
                for item in items:
                    before, after = base(item['z'][None]), initial(item['z'][None])
                    torch.testing.assert_close(after, before, atol=1e-5, rtol=1e-5)
                    initial_difference = max(initial_difference, float((before-after).abs().max()))
                    initial_uv_changes += int(((before[:,-1:] >= 0) != (after[:,-1:] >= 0)).sum())
        if initial_uv_changes:
            raise ValueError('拡張前後で初期の有声判定が変わっています')
        if sum(p.numel() for p in model.parameters()) != info['parameters']:
            raise ValueError('重み数が記録と異なります')
        for m in (model, endpoint):
            for name in STAT_NAMES:
                torch.testing.assert_close(getattr(m, name), getattr(base, name), atol=0, rtol=0)
        logs = [json.loads(x) for x in (folder / 'metrics.jsonl').read_text().splitlines()]
        if [r['step'] for r in logs] != list(range(0, 5001, 100)):
            raise ValueError('比較学習の記録に欠落があります')
        best_score, best_step = min([(r['score'], r['step']) for r in logs] + [(done['endpoint_validation_score'], 5000)])
        if state['step'] != best_step or done['best_step'] != best_step or end_state['step'] != 5000:
            raise ValueError('保存点の選択が異なります')
        np.testing.assert_allclose(state['score'], best_score, atol=1e-8, rtol=1e-8)
        endpoint_score, _ = evaluate(endpoint, data['validation'], acoustic)
        np.testing.assert_allclose(endpoint_score, done['endpoint_validation_score'], atol=1e-5, rtol=1e-5)
        summary = {'best_step': best_step, 'updates': done['updates'], 'checkpoint_sha256': digest(folder / 'best.pt'),
                   'training_seconds': done['training_seconds'], 'initial_validation_score': initial_score,
                   'width': info['width'], 'parameters': info['parameters'], 'conv_macs_per_audio_second': info['conv_macs_per_audio_second'], 'clips': []}
        summary['initial_prediction_max_abs_difference'] = initial_difference
        summary['initial_uv_changes'] = initial_uv_changes
        train_score, train_rows = evaluate(model, data['train'], acoustic)
        summary['train'] = {'score': train_score, 'means': means(train_rows)}
        for split, key in (('validation', 'best_validation_score'), ('test', 'test_score')):
            score, rows = evaluate(model, data[split], acoustic)
            np.testing.assert_allclose(score, done[key], atol=1e-5, rtol=1e-5)
            for actual, saved in zip(rows, done[split + '_rows'], strict=True):
                if actual['id'] != saved['id']:
                    raise ValueError('評価文章が異なります')
                for k in actual:
                    if k != 'id' and actual[k] is not None:
                        np.testing.assert_allclose(actual[k], saved[k], atol=1e-4, rtol=1e-5)
            summary[split] = {'score': score, 'means': means(rows), 'rows': rows}
        for item in data['test']:
            with torch.no_grad():
                features, f0, uv = model.physical(item['z'][None])
            arrays = {k: v[0].numpy() for k, v in {'features': features, 'f0': f0, 'uv_probability': uv}.items()}
            differences = {}
            with np.load(folder / 'predictions' / (item['id'] + '.npz'), allow_pickle=False) as saved:
                if set(saved.files) != set(arrays):
                    raise ValueError('保存した予測項目が異なります')
                for k, a in arrays.items():
                    np.testing.assert_allclose(a, saved[k], atol=1e-4, rtol=1e-4)
                    differences[k] = float(np.max(np.abs(a - saved[k])))
            y = synthesize(arrays['features'], arrays['f0'], item['samples'])
            if not np.isfinite(y).all() or not np.any(y):
                raise ValueError('再合成波形が不正です')
            np.save(analysis / (condition + '_' + item['id'] + '.npy'), y, allow_pickle=False)
            summary['clips'].append({'id': item['id'], 'raw_peak': float(np.abs(y).max()), 'saved_prediction_max_abs_difference': differences})
            if item['id'] == reply_item['id']:
                replies[condition] = y
        report['conditions'][condition] = summary
        summary['reply_per_second'] = windows(model, reply_item, acoustic)
    replies['approved_world_compact'] = pcm(args.reference / 'world_compact_parameters.wav',
        '3bd909da60c80be8a36b4ea13e6784c7574cc893088e62e9aaaf7bfde8b63889')
    replies['previous_context'] = pcm(args.last_listening,
        'dd7d9f3a90365003f2e92f6c95f5e502f6f50c31833f17744869ebd4881728d8')
    if any(x.shape != (282240,) for x in replies.values()):
        raise ValueError('比較音声の長さが異なります')
    save_previews(analysis / 'reply', replies, {'comparison': 'equal RMS, constant gain only'}, pcm_scale=32768)
    stats = {}
    for name in replies:
        p = analysis / 'reply' / (name + '.wav')
        y = pcm(p, digest(p))
        integer = np.rint(y * 32768).astype(np.int32)
        stats[name] = {'sha256': digest(p), 'samples': len(y), 'rms': float(np.sqrt(np.mean(y.astype(float) ** 2))),
                       'peak': float(np.abs(y).max()), 'rails': int(np.count_nonzero((integer == 32767) | (integer == -32768)))}
        if len(y) != 282240 or stats[name]['rails']:
            raise ValueError('試聴WAVが不正です')
    if np.ptp([r['rms'] for r in stats.values()]) > 5e-7:
        raise ValueError('平均音量が一致しません')
    report.update(integrity_passed=True, wav_validation=stats, previous_raw_reproduced=True,
                  last_listening_source_sha256=digest(args.last_listening),
                  candidate_by_validation=min(report['conditions'], key=lambda k: report['conditions'][k]['validation']['score']))
    write_json(analysis / 'audit.json', report)
    print(json.dumps({'integrity_passed': True, 'candidate': report['candidate_by_validation'],
                      'base': report['base']['test']['means'],
                      'conditions': {k: {'step': v['best_step'], 'test': v['test']['means']} for k, v in report['conditions'].items()},
                      'wav_validation': stats}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('result', 'trusted', 'reference', 'previous', 'last-listening'):
        p.add_argument('--' + name, type=Path, required=True)
    run(p.parse_args())
