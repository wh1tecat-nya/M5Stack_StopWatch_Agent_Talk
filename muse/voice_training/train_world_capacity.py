#!/usr/bin/env python3
"""初期出力を保持した容量拡張を、同じ音響損失と更新数で比較する。"""
import argparse
import json
from pathlib import Path
import random
import time

import numpy as np
import torch

from train_world import load_data, export_predictions, digest, write_json
from world_model import objective as legacy_objective
from world_capacity_model import load, save, HALO, WIDTHS
from world_acoustic_loss import AcousticLoss

BASE_SHA = '83c385a3c61cef00c76bf68acd97aa6b9dd4e311366e9e2050324191dacbcd67'
CODE_FILES = ('train_world_capacity.py', 'world_capacity_model.py', 'world_acoustic_loss.py', 'train_world.py', 'world_model.py', 'world_vocoder.py')


def batch_segments(items, rng, batch):
    # 両条件に同じ128フレームを渡し、中央96フレームだけを学習する。
    pieces = {k: [] for k in ('z', 'features', 'log_f0', 'uv')}
    width = 96 + 2 * HALO
    for _ in range(batch):
        item = rng.choice(items)
        start = rng.randrange(item['z'].shape[1] - width + 1)
        for k in pieces:
            pieces[k].append(item[k][:, start:start + width])
    return {k: torch.stack(v) for k, v in pieces.items()}


@torch.no_grad()
def evaluate(model, items, acoustic):
    model.eval()
    rows = []
    for item in items:
        prediction = model(item['z'][None])
        args = (prediction, item['features'][None], item['log_f0'][None], item['uv'][None], model)
        score, _ = acoustic(*args)
        legacy, _ = legacy_objective(*args)
        rows.append({'id': item['id'], 'score': float(score), 'legacy_loss': float(legacy),
                     **acoustic.metrics(*args)})
    return float(np.mean([x['score'] for x in rows])), rows


def train_condition(args, condition, data, device):
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    model, base_state = load(args.base, initial_condition=condition, device=device)
    acoustic = AcousticLoss().to(device)
    folder = args.output / condition
    folder.mkdir(parents=True, exist_ok=False)
    optimizer = torch.optim.AdamW(model.parameters(), lr=.0002, weight_decay=.0001)
    initial, initial_rows = evaluate(model, data['validation'], acoustic)
    best, best_step, updates = initial, 0, 0
    save(folder / 'best.pt', model, condition, 0, initial)
    info = {'condition': condition, 'base_sha256': BASE_SHA, 'base_step': base_state['step'],
            'seed': args.seed, 'steps_requested': args.steps, 'max_seconds': args.max_seconds,
            'learning_rate': .0002, 'batch': 8, 'parameters': sum(p.numel() for p in model.parameters()),
            'selection': 'same decoded acoustic validation score for both conditions',
            'width': WIDTHS[condition], 'halo_frames': HALO, 'segment_frames': 96,
            'initial_prediction_preserved_with_float_tolerance': True,
            'receptive_frames': 27,
            'conv_macs_per_audio_second': sum(m.out_channels*m.in_channels//m.groups*m.kernel_size[0] for m in model.modules() if isinstance(m, torch.nn.Conv1d))*200,
            'normalization': 'frozen base statistics from 72 training utterances',
            'split_ids': {k: [x['id'] for x in v] for k, v in data.items()},
            'code_sha256': {n: digest(Path(__file__).with_name(n)) for n in CODE_FILES},
            'device': torch.cuda.get_device_name() if device == 'cuda' else device,
            'torch': str(torch.__version__), 'numpy': str(np.__version__),
            'text_to_speech_ready': False, 'firmware_changed': False, 'quality_approved': False}
    write_json(folder / 'run.json', info)
    started = time.monotonic()
    with (folder / 'metrics.jsonl').open('x') as log:
        log.write(json.dumps({'step': 0, 'score': initial, 'validation': initial_rows}) + '\n')
        while updates < args.steps and time.monotonic() - started < args.max_seconds:
            model.train()
            batch = batch_segments(data['train'], rng, 8)
            output = model(batch['z'])[:, :, HALO:-HALO]
            target = [batch[k][:, :, HALO:-HALO] for k in ('features', 'log_f0', 'uv')]
            loss_fn = acoustic
            loss, _ = loss_fn(output, *target, model)
            if not torch.isfinite(loss): raise ValueError('学習損失が非有限です')
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5., error_if_nonfinite=True)
            optimizer.step()
            updates += 1
            if updates % 100 == 0 or updates == args.steps:
                score, rows = evaluate(model, data['validation'], acoustic)
                if score < best:
                    best, best_step = score, updates
                    save(folder / 'best.pt', model, condition, updates, score)
                log.write(json.dumps({'step': updates, 'score': score, 'train_loss': float(loss.detach()), 'validation': rows}) + '\n')
                log.flush()
                print('CAPACITY_PROGRESS', condition, updates, round(score, 6), flush=True)
        endpoint, rows = evaluate(model, data['validation'], acoustic)
        if endpoint < best:
            best, best_step = endpoint, updates
            save(folder / 'best.pt', model, condition, updates, endpoint)
        save(folder / 'endpoint.pt', model, condition, updates, endpoint)
    seconds = time.monotonic() - started
    model, _ = load(folder / 'best.pt', device=device)
    validation_score, validation_rows = evaluate(model, data['validation'], acoustic)
    test_score, test_rows = evaluate(model, data['test'], acoustic)
    export_predictions(model, data['test'], folder)
    frozen_keys = ('z_mean', 'z_std', 'y_mean', 'y_std', 'f0_mean', 'f0_std')
    if any(not torch.equal(getattr(model, k).cpu(), base_state['model'][k].cpu()) for k in frozen_keys):
        raise ValueError('固定した統計が変化しました')
    write_json(folder / 'completed.json', {'updates': updates, 'best_step': best_step,
        'initial_validation_score': initial, 'best_validation_score': validation_score,
        'endpoint_validation_score': endpoint, 'test_score': test_score,
        'validation_rows': validation_rows, 'test_rows': test_rows,
        'training_seconds': seconds, 'quality_approved': False, 'firmware_changed': False})
    print('CAPACITY_COMPLETE', condition, updates, best_step, validation_score, flush=True)


def run(args):
    if digest(args.base) != BASE_SHA: raise ValueError('比較の開始モデルが異なります')
    torch.set_num_threads(2)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    if args.device == 'cuda' and not torch.cuda.is_available(): raise RuntimeError('GPUがありません')
    data = load_data(args.data)
    for items in data.values():
        for item in items:
            for k in ('z', 'features', 'log_f0', 'uv'): item[k] = item[k].to(args.device)
    args.output.mkdir(parents=True, exist_ok=False)
    for condition in ('control', 'capacity'):
        train_condition(args, condition, data, args.device)
    write_json(args.output / 'completed.json', {'conditions': ['control', 'capacity'], 'quality_approved': False})


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--device', choices=['cpu', 'cuda'], default='cuda')
    p.add_argument('--steps', type=int, default=5000)
    p.add_argument('--max-seconds', type=int, default=240)
    p.add_argument('--seed', type=int, default=20261115)
    args = p.parse_args()
    if not 1 <= args.steps <= 5000 or not 1 <= args.max_seconds <= 600:
        p.error('試験の上限を超えています')
    run(args)
