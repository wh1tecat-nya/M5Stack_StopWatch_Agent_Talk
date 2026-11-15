"""既存の日本語テキスト特徴からIrodori潜在表現への接続を試す。製品用ではない。"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import time

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frame_features(tokens, boundaries, times):
    tokens = np.asarray(tokens, dtype=np.float32)
    boundaries = np.asarray(boundaries, dtype=np.float64)
    times = np.asarray(times, dtype=np.float64)
    if (tokens.ndim != 2 or tokens.shape[0] != 48 or boundaries.shape != (tokens.shape[1] + 1,)
            or times.ndim != 1 or not np.isfinite(tokens).all() or not np.isfinite(boundaries).all()
            or not np.isfinite(times).all() or np.any(times < 0) or np.any(np.diff(times) < 0)
            or boundaries[0] != 0 or np.any(np.diff(boundaries) <= 0)):
        raise ValueError('Invalid text features or time boundaries')
    index = np.searchsorted(boundaries[1:], times, side='right').clip(0, tokens.shape[1] - 1)
    duration = boundaries[index + 1] - boundaries[index]
    phase = ((times - boundaries[index]) / duration).clip(0, 1)
    position = np.stack([phase, 1 - phase, np.sin(np.pi * phase), np.cos(np.pi * phase),
                         np.log(duration.clip(.005, 3)), np.log1p(times / .04)])
    return np.concatenate([tokens[:, index], position]).astype(np.float32)


def experimental_boundaries(alignment):
    """CTCの中心点を使う仮の教師時刻。発音境界の確定値ではない。"""
    count = len(alignment['token_ids'])
    phones = alignment['phonemes']
    index = np.array([p['token_index'] for p in phones])
    centers = np.array([(p['start_seconds'] + p['end_seconds']) / 2 for p in phones])
    duration = alignment['seconds']
    if (count < 1 or not len(index) or np.any(index < 0) or np.any(index >= count)
            or np.any(np.diff(index) <= 0) or np.any(np.diff(centers) <= 0)
            or not np.isfinite(centers).all() or not np.isfinite(duration) or duration <= 0
            or centers[0] <= 0 or centers[-1] >= duration):
        raise ValueError('Non-monotonic alignment')
    # Controls before/between/after phonemes receive interpolated centers. This
    # is an explicitly experimental target, never a verified duration label.
    x = np.r_[-1, index, count]
    y = np.r_[0, centers, duration]
    centers = np.interp(np.arange(count), x, y)
    result = np.r_[0, (centers[:-1] + centers[1:]) / 2, duration]
    if np.any(np.diff(result) <= 0):
        raise ValueError('Zero-duration experimental token')
    return result


def lexical_selection(alignment):
    boundaries = experimental_boundaries(alignment)
    centers = (boundaries[:-1] + boundaries[1:]) / 2
    ids = alignment['token_ids']
    selected = sorted({0, len(ids) - 1, *(p['token_index'] for p in alignment['phonemes']),
                       *(i for i, item in enumerate(ids) if item['token'] == '_')})
    selected = np.asarray(selected, dtype=np.int64)
    centers = centers[selected]
    bounds = np.r_[0, (centers[:-1] + centers[1:]) / 2, alignment['seconds']]
    return selected, bounds


def phone_selection(alignment):
    """発音と実際の休止だけを展開する。音素間の挿入blankは展開しない。

    時刻は依然としてCTCによる実験値であり、確認済みの発音境界ではない。
    """
    experimental_boundaries(alignment)  # Validate the source before indexing it.
    ids = alignment['token_ids']
    phones = alignment['phonemes']
    indices = np.array([p['token_index'] for p in phones])
    centers = np.array([(p['start_seconds'] + p['end_seconds']) / 2 for p in phones])
    pauses = [i for i in range(1, len(ids))
              if ids[i]['token'] == ids[i - 1]['token'] == '_'
              and indices[0] < i < indices[-1]]
    selected = np.array(sorted({0, len(ids) - 1, *indices, *pauses}), dtype=np.int64)
    selected_centers = np.interp(selected, np.r_[0, indices, len(ids) - 1],
                                 np.r_[0, centers, alignment['seconds']])
    bounds = np.r_[0, (selected_centers[:-1] + selected_centers[1:]) / 2,
                   alignment['seconds']]
    if np.any(np.diff(bounds) <= 0):
        raise ValueError('Invalid phone-only boundaries')
    return selected, bounds


def predicted_phone_boundaries(ids, log_duration):
    """既存の長さ予測だけから時刻を作る。教師音声・CTC時刻は参照しない。"""
    log_duration = np.asarray(log_duration, dtype=np.float64)
    if (len(ids) < 3 or log_duration.shape != (len(ids),)
            or not np.isfinite(log_duration).all() or np.any(np.abs(log_duration) > 20)):
        raise ValueError('Invalid predicted durations')
    controls = {'_', '^', '$', '?', '?!', '?.', '?~', '#', '[', ']'}
    spoken = [i for i, token in enumerate(ids) if token['token'] not in controls]
    if not spoken:
        raise ValueError('No spoken phones')
    pauses = [i for i in range(1, len(ids)) if ids[i]['token'] == ids[i - 1]['token'] == '_'
              and spoken[0] < i < spoken[-1]]
    selected = np.array(sorted({0, len(ids) - 1, *spoken, *pauses}), dtype=np.int64)
    # Positive values: C roundf uses half-away-from-zero, not NumPy rint's
    # half-to-even. Keep the encoder's float32 multiplication as well.
    raw = np.float32(1.2187) * np.exp(log_duration.astype(np.float32))
    frames = np.floor(raw.astype(np.float64) + .5).clip(1, 80)
    ends = np.cumsum(frames) * (256 / 22050)
    if ends[-1] > 60:
        raise ValueError('Predicted utterance exceeds 60 seconds')
    centers = ends - frames * (128 / 22050)
    centers[0], centers[-1] = 0, ends[-1]
    bounds = np.r_[0, (centers[selected[:-1]] + centers[selected[1:]]) / 2, ends[-1]]
    return selected, bounds


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.depth = nn.Conv1d(64, 64, 5, padding=2, groups=64)
        self.expand = nn.Conv1d(64, 128, 1)
        self.reduce = nn.Conv1d(128, 64, 1)

    def forward(self, x):
        return x + .2 * self.reduce(F.silu(self.expand(self.depth(x))))


class TextAdapter(nn.Module):
    def __init__(self, x_mean, x_std, z_mean, z_std):
        super().__init__()
        for name, value in dict(x_mean=x_mean, x_std=x_std, z_mean=z_mean, z_std=z_std).items():
            self.register_buffer(name, value.clone())
        self.input = nn.Conv1d(x_mean.shape[1], 64, 3, padding=1)
        self.blocks = nn.Sequential(Block(), Block(), Block())
        self.output = nn.Conv1d(64, z_mean.shape[1], 1)

    def forward(self, x):
        return self.output(self.blocks(F.silu(self.input((x - self.x_mean) / self.x_std))))

    def physical(self, x):
        return self(x) * self.z_std + self.z_mean


def frozen_projection(vocoder, z):
    # Keep the accepted weights fixed while retaining gradients to its input.
    # EnvelopeRefiner.forward intentionally disables base gradients, so call
    # its two unchanged stages explicitly for this adapter-only experiment.
    return vocoder.from_base(z, vocoder.base(z))


def acoustic_objective(prediction, target):
    voiced = (target[:, 63:64] >= 0).float()
    spectral = F.smooth_l1_loss(prediction[:, :62], target[:, :62])
    pitch = (F.smooth_l1_loss(prediction[:, 62:63], target[:, 62:63], reduction='none') * voiced).sum() / voiced.sum().clamp_min(1)
    uv = F.binary_cross_entropy_with_logits(prediction[:, 63:64], target[:, 63:64].sigmoid())
    return spectral + .2 * pitch + .2 * uv


def load_examples(args):
    data = json.loads((args.world_data / 'dataset.json').read_text())
    encoder = json.loads((args.encoder / 'report.json').read_text())
    encoded = {r['id']: r for r in encoder['rows']}
    result = {k: [] for k in ['train', 'validation', 'test']}
    for row in data['rows']:
        key = row['id']
        if Path(key).name != key or encoded[key]['split'] != row['split']:
            raise ValueError('Unsafe ID or split mismatch')
        alignment = json.loads((args.alignment / key / 'alignment.json').read_text())
        if not alignment['aligned']:
            continue
        path = args.world_data / row['file']
        if sha(path) != row['sha256']:
            raise ValueError('WORLD data changed')
        with np.load(path, allow_pickle=False) as archive:
            full_z = archive['z'].copy()
            z = full_z[:, ::8].copy()  # Existing 200 Hz interpolated latent -> 25 Hz.
        times = np.arange(z.shape[1]) * .04
        tokens = np.fromfile(args.encoder / key / 'tokens.f32', dtype='<f4').reshape(48, -1)
        boundaries = experimental_boundaries(alignment)
        if args.lexical_only:
            selected, boundaries = lexical_selection(alignment)
            tokens = tokens[:, selected]
        x = frame_features(tokens, boundaries, times)
        if args.feature_kind == 'acoustic':
            c = np.fromfile(args.encoder / key / 'acoustic.f32', dtype='<f4').reshape(40, -1)
            frame_times = np.arange(c.shape[1]) * alignment['seconds'] / c.shape[1]
            c = np.stack([np.interp(times, frame_times, channel) for channel in c]).astype(np.float32)
            x = np.concatenate([c, x[-6:]])
        result[row['split']].append({'id': key, 'x': torch.from_numpy(x), 'z': torch.from_numpy(z),
                                     'full_z': torch.from_numpy(full_z),
                                     'review_flags': alignment['review_flags']})
    if any(not rows for rows in result.values()):
        raise ValueError('Empty data split')
    return result


def train(args):
    if not 1 <= args.steps <= 100000:
        raise ValueError('Invalid training step count')
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.manual_seed(14)
    random.seed(14)
    rows = load_examples(args)

    def moments(key):
        x = torch.cat([r[key] for r in rows['train']], dim=1)
        return x.mean(1)[None, :, None], x.std(1, unbiased=False).clamp_min(.01)[None, :, None]

    model = TextAdapter(*moments('x'), *moments('z'))
    if args.initial_checkpoint:
        initial = torch.load(args.initial_checkpoint, map_location='cpu', weights_only=True)
        model.load_state_dict(initial['model'], strict=True)
    vocoder = None
    if args.world_checkpoint:
        if sha(args.world_checkpoint) != '1fd7d966f410f7e29aee6e1ee2f8ad64bfd87cbd8847057be93931d2384616d1':
            raise ValueError('Accepted envelope checkpoint changed')
        from world_envelope_model import load
        vocoder, _ = load(args.world_checkpoint)
        vocoder.requires_grad_(False).eval()
        with torch.no_grad():
            for split in rows.values():
                for row in split:
                    row['world_target'] = frozen_projection(vocoder, row['full_z'][None])
    optimizer = torch.optim.AdamW(model.parameters(), lr=.0002 if vocoder else .001, weight_decay=.0001)
    best = float('inf')
    history = []
    started = time.monotonic()
    for step in range(1, args.steps + 1):
        model.train()
        example = random.choice(rows['train'])
        x, z = example['x'][None], example['z'][None]
        prediction = model(x)
        target = (z - model.z_mean) / model.z_std
        loss = F.mse_loss(prediction, target) + .1 * F.mse_loss(torch.diff(prediction), torch.diff(target))
        if vocoder:
            frames = example['full_z'].shape[1]
            expanded = F.interpolate(prediction * model.z_std + model.z_mean, size=frames,
                                     mode='linear', align_corners=True)
            begin = random.randrange(max(1, frames - 256))
            end = min(frames, begin + 256)
            left, right = max(0, begin - 50), min(frames, end + 50)
            actual = frozen_projection(vocoder, expanded[:, :, left:right])[:, :, begin-left:end-left]
            loss = acoustic_objective(actual, example['world_target'][:, :, begin:end]) + .05 * loss
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        optimizer.step()
        if step % 100 == 0 or step == args.steps:
            model.eval()
            with torch.no_grad():
                scores = []
                for r in rows['validation']:
                    if vocoder:
                        latent = F.interpolate(model.physical(r['x'][None]), size=r['full_z'].shape[1],
                                               mode='linear', align_corners=True)
                        scores.append(acoustic_objective(frozen_projection(vocoder, latent), r['world_target']).item())
                    else:
                        scores.append(F.mse_loss(model(r['x'][None]), (r['z'][None] - model.z_mean) / model.z_std).item())
                score = sum(scores) / len(scores)
            history.append({'step': step, 'validation_score': score})
            if score < best:
                best = score
                torch.save({'format': 'experimental_world_text_adapter_v1', 'model': model.state_dict(),
                            'step': step, 'score': score}, args.output / 'best.pt')
            print(step, score, flush=True)
    state = torch.load(args.output / 'best.pt', map_location='cpu', weights_only=True)
    model.load_state_dict(state['model']); model.eval()
    tests = []
    with torch.no_grad():
        for r in rows['test']:
            z = model.physical(r['x'][None])[0].numpy()
            np.save(args.output / (r['id'] + '.predicted_latent.npy'), z, allow_pickle=False)
            tests.append({'id': r['id'], 'normalized_mse': F.mse_loss(model(r['x'][None]),
                          (r['z'][None] - model.z_mean) / model.z_std).item()})
    report = {'experimental_only': True, 'training_alignment_verified': False,
              'teacher_timing_used_in_test': True, 'arbitrary_text_inference_ready': False,
              'device_integrated': False, 'parameters': sum(p.numel() for p in model.parameters()),
              'feature_kind': args.feature_kind,
              'lexical_only': args.lexical_only,
              'selection_objective': 'frozen_world_acoustic' if vocoder else 'latent_mse',
              'world_checkpoint_sha256': sha(args.world_checkpoint) if vocoder else None,
              'steps': args.steps, 'seconds': time.monotonic() - started,
              'best_validation_score': best, 'tests': tests, 'history': history,
              'dataset_sha256': sha(args.world_data / 'dataset.json'),
              'encoder_report_sha256': sha(args.encoder / 'report.json'),
              'split_counts': {k: len(v) for k, v in rows.items()}}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['world-data', 'encoder', 'alignment', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--steps', type=int, default=2000)
    parser.add_argument('--feature-kind', choices=['token', 'acoustic'], default='token')
    parser.add_argument('--initial-checkpoint', type=Path)
    parser.add_argument('--world-checkpoint', type=Path)
    parser.add_argument('--lexical-only', action='store_true')
    train(parser.parse_args())
