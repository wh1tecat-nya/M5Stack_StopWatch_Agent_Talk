#!/usr/bin/env python3
"""生成結果の完全性を検査する。聴感上の品質を判定するものではない。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import wave

import numpy as np


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root):
    root = Path(root).resolve()
    plan = json.loads((root / 'plan.json').read_text())
    rows = json.loads((root / 'dataset.json').read_text())
    complete = json.loads((root / 'completed.json').read_text())
    expected = {
        profile['id'] + '-' + row['id']: (profile['id'], row)
        for profile in plan['manifest']['profiles'] for row in plan['texts']
    }
    if len(rows) != len(expected) or complete['samples'] != len(expected):
        raise ValueError('生成件数が計画と一致しません')
    if len({r['sample_id'] for r in rows}) != len(rows):
        raise ValueError('重複した生成IDです')
    if {r['sample_id'] for r in rows} != set(expected):
        raise ValueError('生成IDが計画と一致しません')
    reports = []
    for row in rows:
        profile, text = expected[row['sample_id']]
        if (row['profile'], row['text_id'], row['text'], row['split']) != (
                profile, text['id'], text['text'], text['split']):
            raise ValueError('文章・方式・データ区分が計画と一致しません')
        paths = {}
        for key in ('wav', 'latent'):
            path = (root / row[key]).resolve()
            if not path.is_relative_to(root) or sha256(path) != row[key + '_sha256']:
                raise ValueError(f"生成ファイルの照合失敗: {row['sample_id']} {key}")
            paths[key] = path
        with wave.open(str(paths['wav'])) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 48000):
                raise ValueError('音声形式が異なります')
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
        if not pcm.size or not np.any(pcm):
            raise ValueError('空または無音の音声です')
        if abs(pcm.size / 48000 - row['seconds']) > 1 / 48000:
            raise ValueError('音声の長さが記録と異なります')
        latent = np.load(paths['latent'], allow_pickle=False)
        if latent.ndim != 3 or latent.shape[0] != 1 or latent.shape[2] != 32:
            raise ValueError('潜在表現の形状が異なります')
        if latent.dtype != np.float32 or not np.isfinite(latent).all():
            raise ValueError('潜在表現の値が不正です')
        if not row['watermark_applied']:
            raise ValueError('透かし処理が記録されていません')
        numeric = pcm.astype(np.float64)
        reports.append({
            'sample_id': row['sample_id'], 'profile': profile, 'split': row['split'],
            'seconds': pcm.size / 48000,
            'peak_pcm16': int(np.max(np.abs(numeric))),
            'rms_pcm16': float(np.sqrt(np.mean(numeric ** 2))),
            'full_scale_samples': int(np.count_nonzero((pcm == -32768) | (pcm == 32767))),
            'latent_shape': list(latent.shape), 'listening_review': 'pending',
        })
    return {
        'integrity_passed': True, 'perceptual_quality_verified': False,
        'samples': len(reports), 'seconds': sum(r['seconds'] for r in reports),
        'profiles': dict(Counter(r['profile'] for r in reports)),
        'splits': dict(Counter(r['split'] for r in reports)), 'results': reports,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.directory)
    with args.report.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, ensure_ascii=False))
