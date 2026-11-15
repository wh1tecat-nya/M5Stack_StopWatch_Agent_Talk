#!/usr/bin/env python3
"""照合済みのWORLD特徴から小型予測器を学習する限定的な再構成試験。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import time

import numpy as np
import torch

from world_model import WorldPredictor, objective, CONTEXT, SEGMENT
from world_vocoder import SR, FRAME_MS, SP_DIM, AP_DIM, frame_count

DATASET_SHA = 'ab057135575f0295902ea566732de26229e9ccdd8f9e08f9607a6330fbe507e8'
STAT_NAMES = ('z_mean', 'z_std', 'y_mean', 'y_std', 'f0_mean', 'f0_std')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def checkpoint(path, model, step, score):
    temporary = path.with_suffix('.temporary')
    torch.save({'model': {k: v.detach().cpu() for k, v in model.state_dict().items()},
                'step': step, 'score': score}, temporary)
    temporary.replace(path)


def load_data(root):
    if digest(root / 'dataset.json') != DATASET_SHA:
        raise ValueError('照合済みの特徴データではありません')
    audit = json.loads((root / 'audit.json').read_text())
    if not audit['integrity_passed'] or audit['dataset_sha256'] != DATASET_SHA:
        raise ValueError('特徴データの照合が完了していません')
    dataset = json.loads((root / 'dataset.json').read_text())
    expected = {'mf07-' + r['id']: r['split'] for name in ('pilot_texts.json', 'expanded_texts.json')
                for r in json.loads(Path(__file__).with_name(name).read_text())}
    data = {k: [] for k in ('train', 'validation', 'test')}
    seen = set()
    for row in dataset['rows']:
        sid = row['id']
        if sid in seen or row['split'] != expected.get(sid):
            raise ValueError('分割変更または重複があります')
        seen.add(sid)
        path = (root / row['file']).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path) != row['sha256']:
            raise ValueError('特徴ファイルが一致しません')
        with np.load(path, allow_pickle=False) as archive:
            shapes = {'z': (32, row['frames']), 'features': (SP_DIM + AP_DIM, row['frames']),
                      'log_f0': (1, row['frames']), 'uv': (1, row['frames'])}
            if set(archive.files) != set(shapes) or row['frames'] != frame_count(row['samples']):
                raise ValueError('特徴の項目または時間軸が不正です')
            item = {'id': sid, 'split': row['split'], 'samples': row['samples']}
            for k, shape in shapes.items():
                a = archive[k]
                if a.shape != shape or a.dtype != np.float32 or not np.isfinite(a).all():
                    raise ValueError('特徴の形式が不正です')
                item[k] = torch.from_numpy(a.copy())
            if not torch.all((item['uv'] == 0) | (item['uv'] == 1)):
                raise ValueError('有声ラベルが不正です')
            data[row['split']].append(item)
    if seen != set(expected) or {k: len(v) for k, v in data.items()} != {'train': 72, 'validation': 10, 'test': 10}:
        raise ValueError('特徴が不足しています')
    return data


def normalization(train):
    if not train or any(x['split'] != 'train' for x in train):
        raise ValueError('正規化には学習区分だけを使います')
    z = torch.cat([x['z'] for x in train], dim=1)
    y = torch.cat([x['features'] for x in train], dim=1)
    f0 = torch.cat([x['log_f0'][x['uv'] > 0] for x in train])
    if not len(f0):
        raise ValueError('有声区間がありません')
    def moments(a):
        return a.mean(1)[None, :, None], a.std(1, unbiased=False).clamp_min(.01)[None, :, None]
    return (*moments(z), *moments(y), f0.mean(), f0.std(unbiased=False).clamp_min(.01))


def load_model(path, device='cpu'):
    state = torch.load(path, map_location='cpu', weights_only=True)
    model = WorldPredictor(*(state['model'][k] for k in STAT_NAMES))
    model.load_state_dict(state['model'], strict=True)
    return model.to(device).eval(), state


def batch_segments(items, rng, batch):
    pieces = {k: [] for k in ('z', 'features', 'log_f0', 'uv')}
    width = SEGMENT + 2 * CONTEXT
    for _ in range(batch):
        item = rng.choice(items)
        start = rng.randrange(item['z'].shape[1] - width + 1)
        for k in pieces:
            pieces[k].append(item[k][:, start:start + width])
    return {k: torch.stack(v) for k, v in pieces.items()}


@torch.no_grad()
def evaluate(model, items):
    model.eval()
    rows = []
    for item in items:
        output = model(item['z'][None])
        loss, terms = objective(output, item['features'][None], item['log_f0'][None], item['uv'][None], model)
        predicted_log = output[:, -2:-1] * model.f0_std + model.f0_mean
        actual_uv = item['uv'][None] > 0
        predicted_uv = output[:, -1:] >= 0
        both = actual_uv & predicted_uv
        cents = (predicted_log - item['log_f0'][None]).abs() * (1200 / np.log(2))
        rows.append({'id': item['id'], 'loss': float(loss), **{k: float(v) for k, v in terms.items()},
                     'uv_error_fraction': float((actual_uv != predicted_uv).float().mean()),
                     'f0_mae_cents_joint_voiced': float(cents[both].mean()) if both.any() else None,
                     'joint_voiced_frames': int(both.sum())})
    return float(np.mean([r['loss'] for r in rows])), rows


@torch.no_grad()
def export_predictions(model, items, output):
    folder = output / 'predictions'
    folder.mkdir()
    for item in items:
        # 候補の予測経路に教師特徴・音声は渡さない。
        features, f0, uv = model.physical(item['z'][None])
        arrays = {k: v[0].cpu().numpy() for k, v in {'features': features, 'f0': f0, 'uv_probability': uv}.items()}
        if not all(np.isfinite(a).all() for a in arrays.values()):
            raise ValueError('予測に非有限値があります')
        np.savez_compressed(folder / (item['id'] + '.npz'), **arrays)


def run(args):
    torch.set_num_threads(2)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    rng = random.Random(args.seed)
    device = torch.device(args.device)
    if device.type == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('GPUがありません')
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    data = load_data(args.data)
    model = WorldPredictor(*normalization(data['train'])).to(device)
    for items in data.values():
        for item in items:
            for k in ('z', 'features', 'log_f0', 'uv'):
                item[k] = item[k].to(device)
    args.output.mkdir(parents=True, exist_ok=False)
    macs = sum(layer.out_channels * layer.in_channels // layer.groups * layer.kernel_size[0]
               for layer in model.modules() if isinstance(layer, torch.nn.Conv1d))
    info = {'kind': 'codec_latent_to_WORLD_parameter_prediction', 'data_sha256': DATASET_SHA,
            'code_sha256': {name: digest(Path(__file__).with_name(name)) for name in
                            ('train_world.py', 'world_model.py', 'world_vocoder.py')},
            'seed': args.seed, 'steps_requested': args.steps, 'max_seconds': args.max_seconds,
            'batch': args.batch, 'segment': SEGMENT, 'context': CONTEXT, 'frame_period_ms': FRAME_MS,
            'parameters': sum(p.numel() for p in model.parameters()),
            'conv_macs_per_audio_second': macs * 1000 / FRAME_MS,
            'compute_estimate_excludes': 'WORLD synthesis, activations, memory, frontend; not MCU performance',
            'normalization_source': '72 training utterances only; voiced frames only for logF0',
            'selection': 'mean per-utterance validation loss only',
            'split_ids': {k: [x['id'] for x in v] for k, v in data.items()},
            'test_texts_already_used_for_design': True,
            'device': torch.cuda.get_device_name() if device.type == 'cuda' else str(device),
            'torch': str(torch.__version__), 'numpy': str(np.__version__),
            'teacher_parameters_used_at_inference': False, 'text_to_speech_ready': False,
            'firmware_changed': False, 'C152_real_time_verified': False, 'quality_approved': False}
    write_json(args.output / 'run.json', info)
    optimizer = torch.optim.AdamW(model.parameters(), lr=.0005, weight_decay=.0001)
    initial, rows = evaluate(model, data['validation'])
    best, best_step, updates = initial, 0, 0
    checkpoint(args.output / 'best.pt', model, 0, initial)
    started = time.monotonic()
    with (args.output / 'metrics.jsonl').open('x') as log:
        log.write(json.dumps({'step': 0, 'score': initial, 'validation': rows}) + '\n')
        while updates < args.steps and time.monotonic() - started < args.max_seconds:
            model.train()
            batch = batch_segments(data['train'], rng, args.batch)
            output = model(batch['z'])[:, :, CONTEXT:-CONTEXT]
            target = {k: batch[k][:, :, CONTEXT:-CONTEXT] for k in ('features', 'log_f0', 'uv')}
            loss, _ = objective(output, target['features'], target['log_f0'], target['uv'], model)
            if not torch.isfinite(loss):
                raise ValueError('学習誤差が非有限です')
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5., error_if_nonfinite=True)
            optimizer.step()
            updates += 1
            if updates % 100 == 0 or updates == args.steps:
                score, rows = evaluate(model, data['validation'])
                if score < best:
                    best, best_step = score, updates
                    checkpoint(args.output / 'best.pt', model, updates, score)
                log.write(json.dumps({'step': updates, 'train_loss': float(loss.detach()), 'score': score, 'validation': rows}) + '\n')
                log.flush()
                print('WORLD_PROGRESS', updates, round(score, 6), 'best', best_step, flush=True)
        endpoint, rows = evaluate(model, data['validation'])
        if endpoint < best:
            best, best_step = endpoint, updates
            checkpoint(args.output / 'best.pt', model, updates, endpoint)
        checkpoint(args.output / 'endpoint.pt', model, updates, endpoint)
    seconds = time.monotonic() - started
    model, _ = load_model(args.output / 'best.pt', device)
    test_score, test_rows = evaluate(model, data['test'])
    export_predictions(model, data['test'], args.output)
    write_json(args.output / 'completed.json', {'updates': updates, 'best_step': best_step,
        'initial_validation_loss': initial, 'best_validation_loss': best, 'endpoint_validation_loss': endpoint,
        'test_loss': test_score, 'test_rows': test_rows, 'training_seconds': seconds,
        'quality_approved': False, 'text_to_speech_ready': False, 'firmware_changed': False})
    print('WORLD_COMPLETE', updates, best_step, best, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--device', choices=['cpu', 'cuda'], default='cuda')
    p.add_argument('--steps', type=int, default=5000)
    p.add_argument('--max-seconds', type=int, default=240)
    p.add_argument('--batch', type=int, default=8)
    p.add_argument('--seed', type=int, default=20261113)
    args = p.parse_args()
    if not 1 <= args.steps <= 5000 or not 1 <= args.max_seconds <= 600 or not 1 <= args.batch <= 8:
        p.error('試験の上限を超えています')
    run(args)
