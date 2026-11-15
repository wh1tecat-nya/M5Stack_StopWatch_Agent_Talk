#!/usr/bin/env python3
"""診断③を目標に、教師位相を使わず振幅補正を学習する限定試験。"""
import argparse
import json
from pathlib import Path
import random
import time

import numpy as np
import torch

from train_decoder import Reconstruction, load_data, digest, write_json, atomic_checkpoint
from magnitude_model import (MagnitudeCorrection, FrozenPhaseReconstruction, spectrum,
    waveform, unit_phase, match_frames, magnitude_loss, batch_segments,
    convolution_macs_per_frame, SR, HOP, CONTEXT, SEGMENT)

BASE_SHA = 'c7f0140da7eaa75ab6d098f06c90d6356d13bed3fce13bab283776a9d302a542'


def load_model(path, device):
    if digest(path) != BASE_SHA:
        raise ValueError('固定する位相モデルが異なります')
    state = torch.load(path, map_location='cpu', weights_only=True)['model']
    base = Reconstruction(state['mean'], state['std'])
    base.load_state_dict(state, strict=True)
    return FrozenPhaseReconstruction(base, MagnitudeCorrection(base.mean, base.std)).to(device)


@torch.no_grad()
def prepare(items, model, device):
    result = []
    for item in items:
        z = item['z'][None].to(device)
        s = model.base_spectrum(z, item['samples'])
        target = spectrum(item['y'][None, :item['samples']].to(device)).abs()
        assert s.shape == target.shape
        result.append({'id': item['row']['sample_id'], 'z': match_frames(z, s.shape[-1])[0].cpu(),
                       'base': s.abs()[0].cpu(), 'target': target[0].cpu()})
    return result


@torch.no_grad()
def evaluate(head, items, device):
    rows = []
    for item in items:
        z, base, target = (item[k][None].to(device) for k in ('z', 'base', 'target'))
        value = magnitude_loss(head(z, base), target)
        rows.append({'id': item['id'], 'loss': float(value)})
    return float(np.mean([x['loss'] for x in rows])), rows


@torch.no_grad()
def export(model, items, output, device):
    for item in items:
        directory = output / item['row']['sample_id']
        directory.mkdir()
        z = item['z'][None].to(device)
        reference = item['y'][None, :item['samples']].to(device)
        s = model.base_spectrum(z, item['samples'])
        # referenceを受け取らない公開推論経路を実際の候補に使用する。
        predicted = model(z, item['samples'])
        corrected = waveform(model.correction(z, s.abs()) * unit_phase(s), item['samples'])
        torch.testing.assert_close(predicted, corrected)
        audios = {'candidate': waveform(s, item['samples']), 'predicted': predicted,
                  'oracle_magnitude': waveform(spectrum(reference).abs() * unit_phase(s), item['samples']),
                  'reference': reference}
        for name, y in audios.items():
            if not torch.isfinite(y).all():
                raise ValueError('非有限の出力です')
            np.save(directory / (name + '.npy'), y[0].cpu().numpy(), allow_pickle=False)


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
    model = load_model(args.base, device)
    if args.expanded_plan:
        from expanded_data import load_expanded, original_training_rows
        data = load_expanded(args.data, args.expanded_plan)
        scaler_data = original_training_rows(data)
    else:
        data = load_data(args.data)
        scaler_data = data['train']
    # 現モデルの統計は学習用8文から作成されたもの。評価データで更新しない。
    from train_decoder import normalization
    for actual, expected in zip((model.base.mean, model.base.std), normalization(scaler_data)):
        torch.testing.assert_close(actual.cpu(), expected)
    cached = {k: prepare(data[k], model, device) for k in ('train', 'validation')}
    args.output.mkdir(parents=True, exist_ok=False)
    head = model.correction
    optimizer = torch.optim.AdamW(head.parameters(), lr=.0003, weight_decay=.0001)
    run_info = {'kind': 'frozen_candidate_phase_learned_magnitude_codec_reconstruction',
                'source_sha256': digest(__file__),
                'model_code_sha256': digest(Path(__file__).with_name('magnitude_model.py')),
                'base_sha256': BASE_SHA, 'base_step': 2000, 'seed': args.seed,
                'steps_requested': args.steps, 'wall_limit_seconds': args.max_seconds,
                'batch': args.batch, 'segment': SEGMENT, 'context': CONTEXT,
                'sample_rate': SR, 'hop': HOP, 'fft': 1024,
                'trainable_parameters': sum(p.numel() for p in head.parameters()),
                'frozen_parameters': sum(p.numel() for p in model.base.parameters()),
                'extra_conv_macs_per_audio_second': convolution_macs_per_frame(head) * SR / HOP,
                'compute_estimate_excludes': 'base model, STFT/iSTFT, activations, memory and frontend; not a device timing',
                'teacher_waveform_used_for_inference': False,
                'split_ids': {k: [x['row']['sample_id'] for x in v] for k, v in data.items()},
                'split_seconds': {k: sum(x['samples'] / SR for x in v) for k, v in data.items()},
                'checkpoint_selection': 'validation magnitude loss only',
                'expanded_plan_sha256': digest(args.expanded_plan) if args.expanded_plan else None,
                'normalization_source': 'original eight training utterances, frozen',
                'test_texts_already_used_for_design_and_listening': True,
                'device': torch.cuda.get_device_name() if device.type == 'cuda' else str(device),
                'torch': torch.__version__, 'numpy': np.__version__,
                'text_to_speech_ready': False, 'firmware_compatible': False,
                'quality_approved': False}
    write_json(args.output / 'run.json', run_info)
    initial, rows = evaluate(head, cached['validation'], device)
    best, best_step, updates = initial, 0, 0
    atomic_checkpoint(args.output / 'best.pt', {'correction': head.state_dict(), 'step': 0, 'score': best})
    print('MAGNITUDE_START', json.dumps(run_info), flush=True)
    started = time.monotonic()
    with (args.output / 'metrics.jsonl').open('x') as log:
        log.write(json.dumps({'step': 0, 'score': initial, 'validation': rows}) + '\n')
        while updates < args.steps and time.monotonic() - started < args.max_seconds:
            z, base, target = batch_segments(cached['train'], rng, args.batch, device)
            prediction = head(z, base)[:, :, CONTEXT:CONTEXT + SEGMENT]
            loss = magnitude_loss(prediction, target[:, :, CONTEXT:CONTEXT + SEGMENT])
            if not torch.isfinite(loss):
                raise ValueError('学習誤差が非有限です')
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(head.parameters(), 5., error_if_nonfinite=True)
            optimizer.step()
            updates += 1
            if updates % 100 == 0 or updates == args.steps:
                score, rows = evaluate(head, cached['validation'], device)
                if score < best:
                    best, best_step = score, updates
                    atomic_checkpoint(args.output / 'best.pt', {'correction': head.state_dict(), 'step': updates, 'score': score})
                log.write(json.dumps({'step': updates, 'train_loss': float(loss.detach()), 'score': score, 'validation': rows}) + '\n')
                log.flush()
                print('PROGRESS', updates, 'validation', round(score, 6), 'best', best_step, flush=True)
        endpoint_score, _ = evaluate(head, cached['validation'], device)
        atomic_checkpoint(args.output / 'endpoint.pt', {'correction': head.state_dict(), 'step': updates, 'score': endpoint_score})
    training_seconds = time.monotonic() - started
    best_state = torch.load(args.output / 'best.pt', map_location=device, weights_only=True)
    head.load_state_dict(best_state['correction'], strict=True)
    test_cache = prepare(data['test'], model, device)
    test_score, test_rows = evaluate(head, test_cache, device)
    model.eval()
    export(model, data['test'], args.output, device)
    # 学習前後の固定モデルを比較し、位相が更新されていないことを確認する。
    old = torch.load(args.base, map_location='cpu', weights_only=True)['model']
    if any(not torch.equal(v.cpu(), old[k]) for k, v in model.base.state_dict().items()):
        raise AssertionError('固定モデルが変化しました')
    write_json(args.output / 'completed.json', {'updates': updates, 'best_step': best_step,
        'initial_validation_loss': initial, 'best_validation_loss': best,
        'endpoint_validation_loss': endpoint_score, 'test_loss': test_score, 'test_rows': test_rows,
        'training_seconds': training_seconds, 'frozen_base_unchanged': True,
        'quality_verified': False, 'text_to_speech_ready': False, 'firmware_changed': False})
    print('MAGNITUDE_COMPLETE', updates, best_step, best, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cuda')
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--steps', type=int, default=3000)
    parser.add_argument('--max-seconds', type=int, default=240)
    parser.add_argument('--batch', type=int, default=8)
    parser.add_argument('--expanded-plan', type=Path)
    args = parser.parse_args()
    if not 1 <= args.steps <= 5000 or not 1 <= args.max_seconds <= 600 or not 1 <= args.batch <= 8:
        parser.error('試験の上限を超えています')
    run(args)
