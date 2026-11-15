#!/usr/bin/env python3
"""WORLD特徴92件の出所・時間軸・分割・有限値を独立に照合する。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
from world_vocoder import SR, FRAME_MS, SP_DIM, AP_DIM, frame_count


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    dataset_path = args.features / 'dataset.json'
    dataset = json.loads(dataset_path.read_text())
    source = json.loads((args.source / 'dataset.json').read_text())
    source = {r['sample_id']: r for r in source}
    expected = {'mf07-' + r['id']: r for name in ('pilot_texts.json', 'expanded_texts.json')
                for r in json.loads(Path(__file__).with_name(name).read_text())}
    if set(source) != set(expected) or len(source) != 92:
        raise ValueError('元データと文章計画が一致しません')
    for filename, key in (('prepare_world.py', 'code_sha256'), ('world_vocoder.py', 'vocoder_code_sha256')):
        if digest(Path(__file__).with_name(filename)) != dataset[key]:
            raise ValueError('特徴抽出後に処理コードが変わっています')
    if (dataset['sr'], dataset['frame_period_ms']) != (SR, FRAME_MS):
        raise ValueError('特徴の時間軸が異なります')
    seen, rows = set(), dataset['rows']
    if Counter(r['split'] for r in rows) != {'train': 72, 'validation': 10, 'test': 10}:
        raise ValueError('データ分割が異なります')
    for row in rows:
        sid = row['id']
        if sid in seen or sid not in expected:
            raise ValueError('IDの重複か不足があります')
        seen.add(sid)
        original, planned = source[sid], expected[sid]
        if original['text'] != planned['text'] or row['split'] != planned['split'] or original['split'] != planned['split']:
            raise ValueError('文章・分割が変更されています')
        for key in ('wav', 'latent'):
            path = (args.source / original[key]).resolve()
            if not path.is_relative_to(args.source.resolve()) or digest(path) != row['source_' + key + '_sha256']:
                raise ValueError('音声・潜在表現のSHAが一致しません')
        path = (args.features / row['file']).resolve()
        if not path.is_relative_to(args.features.resolve()) or digest(path) != row['sha256']:
            raise ValueError('特徴のSHAが一致しません')
        latent = np.load(args.source / original['latent'], allow_pickle=False)[0]
        if row['samples'] != len(latent) * 882 or row['frames'] != frame_count(row['samples']):
            raise ValueError('入力音声と特徴の長さが一致しません')
        with np.load(path, allow_pickle=False) as data:
            shapes = {'z': (32, row['frames']), 'features': (SP_DIM + AP_DIM, row['frames']),
                      'uv': (1, row['frames']), 'log_f0': (1, row['frames'])}
            if set(data.files) != set(shapes):
                raise ValueError('特徴の項目が不正です')
            for name, shape in shapes.items():
                if data[name].shape != shape or data[name].dtype != np.float32 or not np.isfinite(data[name]).all():
                    raise ValueError('特徴の形状・形式・有限値の検査に失敗しました')
            if not np.isin(data['uv'], [0., 1.]).all():
                raise ValueError('有声ラベルが不正です')
            # 抽出処理とは別の補間APIでフレーム中心を再検査する。
            native_times = (np.arange(len(latent)) + .5) / 25
            target_times = np.arange(row['frames']) * FRAME_MS / 1000
            aligned = np.array([np.interp(target_times, native_times, latent[:, i]) for i in range(32)])
            np.testing.assert_allclose(data['z'], aligned, atol=1e-5, rtol=1e-6)
    if seen != set(expected):
        raise ValueError('特徴が不足しています')
    report = {'integrity_passed': True, 'files': len(rows), 'splits': dict(Counter(r['split'] for r in rows)),
              'seconds': sum(r['samples'] for r in rows) / SR, 'dataset_sha256': digest(dataset_path),
              'audit_code_sha256': digest(__file__), 'new_training_performed': False,
              'subjective_quality_approved': False}
    (args.features / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--features', type=Path, required=True)
    p.add_argument('--source', type=Path, required=True)
    run(p.parse_args())
