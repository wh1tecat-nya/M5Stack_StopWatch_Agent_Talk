#!/usr/bin/env python3
"""同じ文章上で旧補正と拡充後補正を比較し、目標③を含む試聴音声を作る。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

import numpy as np
import torch
from compare_band_results import save_previews

OLD_SHA = 'c7102d92e55da692b30c3038c3d29cf105a9c34419d00d7007fc66887160b506'
TARGET_SHA = 'fb2eead93b998c1e3f5b15838091fa102e203dd7a8bb6d1e5f4d0e597247ef33'


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(args):
    if not json.loads((args.result / 'analysis/audit.json').read_text())['integrity_passed']:
        raise ValueError('入力照合とCPU再生成の確認が必要です')
    assert digest(args.previous) == OLD_SHA
    assert digest(args.target) == TARGET_SHA
    sys.path.insert(0, str(args.trusted_input.resolve()))
    import train_magnitude as t
    from expanded_data import load_expanded
    torch.set_num_threads(2)
    model = t.load_model(args.trusted_input / 'base.pt', 'cpu').eval()
    data = load_expanded(args.result / 'data', args.trusted_input / 'expanded_texts.json')
    cache = {k: t.prepare(items, model, 'cpu') for k, items in data.items()}
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'quality_verified': False, 'firmware_changed': False,
              'previous_sha256': OLD_SHA, 'target_3_sha256': TARGET_SHA, 'conditions': {}}
    outputs = {}
    with torch.no_grad():
        for condition, checkpoint in [('previous', args.previous), ('expanded', args.result / 'output/best.pt')]:
            state = torch.load(checkpoint, map_location='cpu', weights_only=True)
            model.correction.load_state_dict(state['correction'], strict=True)
            scores = {}
            for split, rows in cache.items():
                for subset in ('original', 'additional'):
                    selected = [r for r in rows if r['id'].startswith('mf07-expand_') == (subset == 'additional')]
                    score, detail = t.evaluate(model.correction, selected, 'cpu')
                    scores[split + '_' + subset] = {'loss': score, 'rows': detail}
            report['conditions'][condition] = {'checkpoint_sha256': digest(checkpoint),
                'step': state['step'], 'scores': scores}
            outputs[condition] = {r['row']['sample_id']: model(r['z'][None], r['samples'])[0].numpy()
                                  for r in data['test']}
    with wave.open(str(args.target)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (22050, 1, 2)
        target = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768
    reply_id = 'mf07-test_reply'
    assert target.shape == outputs['expanded'][reply_id].shape
    steps = {c: r['step'] for c, r in report['conditions'].items()}
    save_previews(args.output / 'reply', {c: r[reply_id] for c, r in outputs.items()} | {'quality_target_3': target}, steps)
    # 初めて使う長文も保存する。重み選択や追加学習には使用しない。
    held = next(r for r in data['test'] if r['row']['sample_id'] == 'mf07-expand_test_08')
    name = held['row']['sample_id']
    save_previews(args.output / 'held_out', {c: r[name] for c, r in outputs.items()} |
                  {'teacher_reference': held['y'][:held['samples']].numpy()}, steps)
    report['held_out_text'] = held['row']['text']
    for directory in ('reply', 'held_out'):
        rmss = []
        for p in (args.output / directory).glob('*.wav'):
            with wave.open(str(p)) as w:
                assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (22050, 1, 2)
                y = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float) / 32768
                assert np.max(np.abs(y)) <= .9001
                rmss.append(float(np.sqrt(np.mean(y * y))))
        assert max(rmss) - min(rmss) < 2e-6
    (args.output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({c: {k: v['loss'] for k, v in r['scores'].items()} for c, r in report['conditions'].items()}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('result', type=Path)
    p.add_argument('--trusted-input', type=Path, required=True)
    p.add_argument('--previous', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    run(p.parse_args())
