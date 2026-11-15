#!/usr/bin/env python3
"""合格済み包絡予測器の出力を教師にした、文章からの音響予測試作。"""
import argparse
import json
from pathlib import Path
import random
import time

import numpy as np
import torch
import torch.nn.functional as F

from world_envelope_model import load
from world_text_adapter import TextAdapter, acoustic_objective, frame_features, frozen_projection, lexical_selection, phone_selection, sha


def run(args):
    if not 1 <= args.steps <= 100000:
        raise ValueError('Invalid training steps')
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2); torch.manual_seed(14); random.seed(14)
    cache = Path(__file__).resolve().parents[1] / '.cache'
    root = cache / 'irodori-text-frontend-20261114'
    checkpoint = cache / 'irodori-world-envelope-baseline-20261113/baseline.pt'
    if sha(checkpoint) != '1fd7d966f410f7e29aee6e1ee2f8ad64bfd87cbd8847057be93931d2384616d1':
        raise ValueError('Accepted voice changed')
    teacher, _ = load(checkpoint); teacher.requires_grad_(False).eval()
    data_root = cache / 'irodori-world-features-20261112-final'
    dataset = json.loads((data_root / 'dataset.json').read_text())
    data = {k: [] for k in ['train', 'validation', 'test']}
    for row in dataset['rows']:
        key = row['id']; path = data_root / row['file']
        if sha(path) != row['sha256']:
            raise ValueError('Teacher data changed')
        with np.load(path, allow_pickle=False) as archive:
            z = torch.from_numpy(archive['z'].copy())[None]
        with torch.no_grad():
            target = frozen_projection(teacher, z)[0, :, ::2].clone()  # 100 Hz.
        alignment = json.loads((root / 'all-utterances' / key / 'alignment.json').read_text())
        token = np.fromfile(root / 'token-features' / key / 'tokens.f32', dtype='<f4').reshape(48, -1)
        selected, bounds = (phone_selection if args.phone_only else lexical_selection)(alignment)
        x = frame_features(token[:, selected], bounds, np.arange(target.shape[1]) * .01)
        data[row['split']].append({'id': key, 'x': torch.from_numpy(x), 'target': target,
                                  'full_frames': z.shape[2]})

    def moments(key):
        x = torch.cat([r[key] for r in data['train']], dim=1)
        return x.mean(1)[None, :, None], x.std(1, unbiased=False).clamp_min(.01)[None, :, None]

    model = TextAdapter(*moments('x'), *moments('target'))
    optimizer = torch.optim.AdamW(model.parameters(), lr=.0005, weight_decay=.0001)
    best = float('inf'); history = []; started = time.monotonic()
    for step in range(1, args.steps + 1):
        model.train(); row = random.choice(data['train'])
        target = row['target'][None]
        prediction = model.physical(row['x'][None])
        loss = acoustic_objective(prediction, target) + .1 * F.smooth_l1_loss(
            torch.diff(prediction[:, :62]), torch.diff(target[:, :62]))
        optimizer.zero_grad(); loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        optimizer.step()
        if step % 100 == 0 or step == args.steps:
            model.eval()
            with torch.no_grad():
                score = sum(acoustic_objective(model.physical(r['x'][None]), r['target'][None]).item()
                            for r in data['validation']) / len(data['validation'])
            history.append({'step': step, 'validation_acoustic_score': score})
            if score < best:
                best = score
                torch.save({'format': 'experimental_world_text_direct_v1', 'model': model.state_dict(),
                            'step': step, 'score': score}, args.output / 'best.pt')
            print(step, score, flush=True)
    state = torch.load(args.output / 'best.pt', map_location='cpu', weights_only=True)
    model.load_state_dict(state['model']); model.eval(); tests = []
    with torch.no_grad():
        for row in data['test']:
            predicted = model.physical(row['x'][None])
            score = acoustic_objective(predicted, row['target'][None]).item()
            full = F.interpolate(predicted, size=row['full_frames'], mode='linear', align_corners=True)
            coded = full[:, :62] * teacher.y_std + teacher.y_mean
            uv = full[:, 63:64].sigmoid()
            f0 = torch.exp((full[:, 62:63] * teacher.f0_std + teacher.f0_mean).clamp(4.174387, 6.39693)) * (uv >= .5)
            physical = torch.cat([coded, f0, uv], dim=1)[0].numpy().astype('<f4')
            physical[60:62] = np.clip(physical[60:62], -60, -.001)
            physical.tofile(args.output / (row['id'] + '.physical.f32'))
            tests.append({'id': row['id'], 'frames': row['full_frames'], 'acoustic_score': score})
    report = {'experimental_only': True, 'teacher_timing_used': True, 'arbitrary_text_ready': False,
              'device_integrated': False, 'baseline_weights_modified': False,
              'teacher_checkpoint_sha256': sha(checkpoint), 'parameters': sum(p.numel() for p in model.parameters()),
              'inference_hz': 100, 'phone_only': args.phone_only,
              'seconds': time.monotonic() - started, 'steps': args.steps,
              'best_validation_score': best, 'history': history, 'tests': tests,
              'alignment_verified': False, 'split_counts': {k: len(v) for k, v in data.items()}}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=2000)
    parser.add_argument('--phone-only', action='store_true')
    run(parser.parse_args())
