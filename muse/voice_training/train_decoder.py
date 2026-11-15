#!/usr/bin/env python3
"""Bのcodec潜在表現から小型decoderを学習する限定的な再構成試験。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import wave

import numpy as np
from scipy.signal import resample_poly
import torch
from torch import nn
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'vendor'))
from saanotts_jp._param_reference import Decoder
from saanotts_jp.discriminator import FirstDifferenceDiscriminator
from saanotts_jp.losses import generator_loss, discriminator_loss
from band_loss import balanced_spectral_loss

SR, HOP, CONTEXT, SEGMENT = 22050, 256, 20, 64
INITIAL_SHA = 'be5bf3f586cc6de741f91063fcf06e0b4a47ffce7f9e27cf5bd32e39fbfadcc6'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def align_latent(latent, frames):
    """25fpsのフレーム中心を、hop256のフレーム中心へ線形補間する。"""
    positions = ((np.arange(frames) + .5) * HOP / SR * 25 - .5)
    positions = np.clip(positions, 0, len(latent) - 1)
    left = positions.astype(np.int64)
    right = np.minimum(left + 1, len(latent) - 1)
    mix = (positions - left).astype(np.float32)[:, None]
    return ((1 - mix) * latent[left] + mix * latent[right]).T.copy()


def load_data(root, expected_counts=None):
    rows = json.loads((root / 'dataset.json').read_text())
    splits = {name: [] for name in ('train', 'validation', 'test')}
    ids, texts = set(), set()
    for row in rows:
        if row['profile'] != 'mf07' or row['sample_id'] in ids or row['text'] in texts:
            raise ValueError('B以外・ID重複・同一文章の重複が混在しています')
        ids.add(row['sample_id'])
        texts.add(row['text'])
        paths = {}
        for key in ('wav', 'latent'):
            path = (root / row[key]).resolve()
            if not path.is_relative_to(root.resolve()) or digest(path) != row[key + '_sha256']:
                raise ValueError('入力のSHA-256または保存先が不正です')
            paths[key] = path
        with wave.open(str(paths['wav'])) as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (48000, 1, 2):
                raise ValueError('教師音声の形式が異なります')
            y48 = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768
        storage_gain = row.get('storage_gain', 1.)
        if not math.isfinite(storage_gain) or not .245 <= storage_gain <= 1:
            raise ValueError('PCM保存ゲインが不正です')
        y48 /= storage_gain
        z = np.load(paths['latent'], allow_pickle=False)
        if z.ndim != 3 or z.shape[0] != 1 or z.shape[2] != 32 or not np.isfinite(z).all():
            raise ValueError('潜在表現が不正です')
        if len(y48) != z.shape[1] * 1920:
            raise ValueError('潜在表現と音声の時間軸が一致しません')
        y = resample_poly(y48, 147, 320).astype(np.float32)
        frames = math.ceil(len(y) / HOP)
        item = {'row': row, 'z': torch.from_numpy(align_latent(z[0], frames)),
                'y': torch.from_numpy(np.pad(y, (0, frames * HOP - len(y)))),
                'samples': len(y)}
        splits[row['split']].append(item)
    expected_counts = expected_counts or {'train': 8, 'validation': 2, 'test': 2}
    if {k: len(v) for k, v in splits.items()} != expected_counts:
        raise ValueError('今回の試作データ分割が異なります')
    return splits


class Reconstruction(nn.Module):
    def __init__(self, mean, std):
        super().__init__()
        self.register_buffer('mean', mean)
        self.register_buffer('std', std)
        self.adapter = nn.Sequential(nn.Conv1d(32, 64, 1), nn.Tanh(), nn.Conv1d(64, 40, 1))
        self.decoder = Decoder()

    def forward(self, z):
        c = self.adapter((z - self.mean) / self.std)
        return self.decoder.istft(*self.decoder(c))


def normalization(train):
    # 評価データの分布を学習側へ漏らさない。
    z = torch.cat([x['z'] for x in train], dim=1)
    return z.mean(1)[None, :, None], z.std(1, unbiased=False).clamp_min(.01)[None, :, None]


def batch_segments(items, rng, batch_size, device):
    zs, ys = [], []
    width = SEGMENT + 2 * CONTEXT
    for _ in range(batch_size):
        item = rng.choice(items)
        start = rng.randrange(item['z'].shape[1] - width + 1)
        zs.append(item['z'][:, start:start + width])
        first = (start + CONTEXT) * HOP
        ys.append(item['y'][first:first + SEGMENT * HOP])
    return torch.stack(zs).to(device), torch.stack(ys).to(device)


@torch.no_grad()
def evaluate(model, items, device):
    model.eval()
    rows = []
    for item in items:
        y = model(item['z'][None].to(device))[:, :item['samples']]
        target = item['y'][None, :item['samples']].to(device)
        loss, terms = generator_loss(y, target)
        rows.append({'id': item['row']['sample_id'], 'loss': float(loss),
                     'stft': float(terms['gen/stft']), 'l1': float(terms['gen/l1']),
                     'band_loss': float(balanced_spectral_loss(y, target)),
                     'peak': float(y.abs().max())})
    model.train()
    return float(np.mean([x['loss'] for x in rows])), rows


def atomic_checkpoint(path, value):
    temp = path.with_suffix('.tmp')
    torch.save(value, temp)
    os.replace(temp, path)


def train(args):
    torch.set_num_threads(2)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    rng = random.Random(args.seed)
    device = torch.device(args.device)
    if device.type == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('GPUが接続されていません')
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    if digest(args.initial) != INITIAL_SHA:
        raise ValueError('初期モデルのSHA-256が異なります')
    data = load_data(args.data)
    mean, std = normalization(data['train'])
    model = Reconstruction(mean, std).to(device)
    initial = torch.load(args.initial, map_location='cpu', weights_only=True)
    model.decoder.load_state_dict(initial['state']['decoder'], strict=True)
    disc = FirstDifferenceDiscriminator().to(device)
    optimizer = torch.optim.AdamW([
        {'params': model.adapter.parameters(), 'lr': .001},
        {'params': model.decoder.parameters(), 'lr': .0001}], betas=(.8, .99), weight_decay=.0001)
    opt_disc = torch.optim.AdamW(disc.parameters(), lr=.0001, betas=(.8, .99))
    args.output.mkdir(parents=True, exist_ok=False)
    run = {'kind': 'codec_latent_reconstruction_only', 'baseline': 'mf07',
           'objective': args.objective, 'band_loss_weight': .1 if args.objective == 'bands' else 0.,
           'band_loss_code_sha256': digest(HERE / 'band_loss.py'),
           'source_code_sha256': digest(__file__), 'seed': args.seed,
           'steps_requested': args.steps, 'wall_limit_seconds': args.max_seconds,
           'sample_rate': SR, 'hop': HOP, 'batch': args.batch, 'segment': SEGMENT,
           'context': CONTEXT, 'warmup_steps': 200, 'initial_sha256': INITIAL_SHA,
           'trainable_parameters': sum(p.numel() for p in model.parameters()),
           'decoder_parameters': sum(p.numel() for p in model.decoder.parameters()),
           'torch': torch.__version__, 'numpy': np.__version__,
           'device': torch.cuda.get_device_name() if device.type == 'cuda' else str(device),
           'split_ids': {k: [x['row']['sample_id'] for x in v] for k, v in data.items()},
           'split_seconds': {k: sum(x['samples'] / SR for x in v) for k, v in data.items()},
           'test_used_for_checkpoint_selection': False,
           'evaluation_texts_have_informed_experiment_design': True,
           'alignment': '25fps frame centers linearly interpolated to hop256 frame centers; codec delay not independently measured',
           'firmware_compatible': False, 'perceptual_quality_verified': False}
    write_json(args.output / 'run.json', run)
    best, initial_rows = evaluate(model, data['validation'], device)
    best_step = 0
    atomic_checkpoint(args.output / 'best.pt', {'model': model.state_dict(), 'step': 0, 'validation_loss': best})
    print('TRAIN_START', json.dumps(run, ensure_ascii=False), flush=True)
    start = time.monotonic()
    step = 0
    try:
        with (args.output / 'metrics.jsonl').open('x') as log:
            log.write(json.dumps({'step': 0, 'validation': initial_rows, 'score': best}) + '\n')
            for step in range(1, args.steps + 1):
                if time.monotonic() - start > args.max_seconds:
                    break
                model.train()
                z, y = batch_segments(data['train'], rng, args.batch, device)
                full = model(z)
                fake = full[:, CONTEXT * HOP:(CONTEXT + SEGMENT) * HOP]
                if step > 200:
                    disc.train().requires_grad_(True)
                    opt_disc.zero_grad(set_to_none=True)
                    dloss, _ = discriminator_loss(disc(y)[0], disc(fake.detach())[0])
                    if not torch.isfinite(dloss): raise FloatingPointError('判別器が非有限値です')
                    dloss.backward()
                    nn.utils.clip_grad_norm_(disc.parameters(), 10, error_if_nonfinite=True)
                    opt_disc.step()
                    disc.eval().requires_grad_(False)
                    with torch.no_grad(): _, real_features = disc(y)
                    prediction, fake_features = disc(fake)
                    loss, terms = generator_loss(fake, y, prediction, fake_features, real_features)
                else:
                    loss, terms = generator_loss(fake, y)
                if args.objective == 'bands':
                    band_loss = balanced_spectral_loss(fake, y)
                    loss = loss + .1 * band_loss
                    terms['gen/bands'] = band_loss.detach()
                if not torch.isfinite(loss): raise FloatingPointError('生成器が非有限値です')
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 10, error_if_nonfinite=True)
                optimizer.step()
                if step % 100 == 0 or step == args.steps:
                    score, validation = evaluate(model, data['validation'], device)
                    if not math.isfinite(score): raise FloatingPointError('検証値が不正です')
                    if score < best:
                        best, best_step = score, step
                        atomic_checkpoint(args.output / 'best.pt', {
                            'model': model.state_dict(), 'step': step, 'validation_loss': score})
                    entry = {'step': step, 'elapsed': time.monotonic() - start,
                             'loss': float(loss.detach()), 'terms': {k: float(v) for k, v in terms.items()},
                             'validation': validation, 'score': score, 'best_step': best_step}
                    log.write(json.dumps(entry, allow_nan=False) + '\n'); log.flush()
                    atomic_checkpoint(args.output / 'last.pt', {
                        'model': model.state_dict(), 'discriminator': disc.state_dict(),
                        'optimizer': optimizer.state_dict(), 'optimizer_disc': opt_disc.state_dict(),
                        'step': step, 'torch_rng': torch.get_rng_state(), 'random_rng': rng.getstate()})
                    print('PROGRESS', step, 'validation', round(score, 6), 'best', best_step, flush=True)
    finally:
        write_json(args.output / 'training_stop.json', {'last_iteration': step, 'elapsed': time.monotonic() - start})
    # 同じ更新回数での比較用にも残す。最良重みの選択基準は従来の検証損失で共通。
    atomic_checkpoint(args.output / 'endpoint.pt', {'model': model.state_dict(), 'step': step})
    checkpoint = torch.load(args.output / 'best.pt', map_location=device, weights_only=True)
    model.load_state_dict(checkpoint['model']); model.eval()
    test_score, test_rows = evaluate(model, data['test'], device)
    model.eval()
    samples = []
    with torch.no_grad():
        for item in data['test']:
            raw = model(item['z'][None].to(device))[0, :item['samples']].cpu().numpy()
            reference = item['y'][:item['samples']].numpy()
            # 両音声へ同じ固定ゲインを適用し、比較用WAVのクリップだけを防ぐ。
            peak = max(float(np.max(np.abs(raw))), float(np.max(np.abs(reference))))
            gain = min(1., .90 / max(peak, 1e-6))
            target = args.output / item['row']['sample_id']; target.mkdir()
            np.save(target / 'reconstruction_float32.npy', raw)
            for name, audio in [('reference', reference), ('reconstruction', raw)]:
                with wave.open(str(target / (name + '.wav')), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(SR)
                    wav.writeframes(np.rint(audio * gain * 32767).astype('<i2').tobytes())
            samples.append({'id': item['row']['sample_id'], 'seconds': len(raw) / SR,
                            'shared_preview_gain': gain, 'raw_peak': float(np.max(np.abs(raw)))})
    write_json(args.output / 'completed.json', {
        'best_step': checkpoint['step'], 'validation_loss': checkpoint['validation_loss'],
        'test_loss': test_score, 'test_rows': test_rows, 'samples': samples,
        'initial_validation_loss': float(np.mean([x['loss'] for x in initial_rows])),
        'perceptual_quality_verified': False, 'text_to_speech_ready': False,
        'firmware_compatible': False, 'training_seconds': time.monotonic() - start})
    print('RECONSTRUCTION_COMPLETE', checkpoint['step'], test_score, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--initial', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=2000)
    parser.add_argument('--max-seconds', type=int, default=900)
    parser.add_argument('--batch', type=int, default=8)
    parser.add_argument('--seed', type=int, default=20261110)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cuda')
    parser.add_argument('--objective', choices=['baseline', 'bands'], default='baseline')
    args = parser.parse_args()
    if not 1 <= args.steps <= 2000 or not 1 <= args.max_seconds <= 900 or not 1 <= args.batch <= 8:
        parser.error('試作上限を超えています')
    train(args)
