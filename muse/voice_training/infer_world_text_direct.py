#!/usr/bin/env python3
"""教師音声を使わず、文章から新音声を生成するホスト試作。実機用ではない。"""
import argparse
import json
from pathlib import Path
import subprocess
import time

import numpy as np
from scipy.io import wavfile
import torch
import torch.nn.functional as F

from world_text_adapter import TextAdapter, frame_features, predicted_phone_boundaries, sha


def run(args):
    torch.set_num_threads(2)
    cache = Path(__file__).resolve().parents[1] / '.cache'
    encoder = cache / 'irodori-text-frontend-20261114/encoder-native'
    voice = cache / 'irodori-world-envelope-baseline-20261113/baseline.pt'
    dictionary = cache / 'dict.bin'
    original = cache / 'audio-reference/model-v4-fp32.bin'
    for path, expected in [(dictionary, '284eb31a4699598ffe885d1623a7215199ff610163c34ded959b0baa6cc7c925'),
                           (original, 'ccbded9f9846a1f2fa429abd7c4ca9e5271363d098b896cd7e02fbcc421744b9'),
                           (voice, '1fd7d966f410f7e29aee6e1ee2f8ad64bfd87cbd8847057be93931d2384616d1')]:
        if sha(path) != expected:
            raise ValueError('Pinned input changed: ' + path.name)
    info = json.loads((args.checkpoint.parent / 'report.json').read_text())
    if not info.get('phone_only') or info.get('inference_hz') != 100:
        raise ValueError('Expected phone-only 100 Hz checkpoint')
    saved = torch.load(args.checkpoint, map_location='cpu', weights_only=True)
    if saved['format'] != 'experimental_world_text_direct_v1':
        raise ValueError('Wrong checkpoint format')
    state = saved['model']
    model = TextAdapter(*(state[k] for k in ['x_mean', 'x_std', 'z_mean', 'z_std']))
    model.load_state_dict(state); model.eval()
    # The accepted predictor is used for its normalization constants only.
    # No teacher latent, waveform, duration or alignment enters this inference.
    from world_envelope_model import load
    norm, _ = load(voice)
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = subprocess.run([str(encoder / 'world_text_cli'), str(dictionary), str(args.text.resolve())],
                            text=True, capture_output=True, check=True, timeout=30)
    ids = [{'id': int(line.split('\t')[0]), 'token': line.split('\t')[1]}
           for line in result.stdout.splitlines()]
    id_path = args.output / 'ids.i32'
    np.array([item['id'] for item in ids], dtype='<i4').tofile(id_path)
    encoded = args.output / 'encoder'
    result = subprocess.run([str(encoder / 'world_text_features_cli'), str(original), str(id_path),
                             str(encoded)], text=True, capture_output=True, check=True, timeout=30)
    tokens = np.fromfile(encoded / 'tokens.f32', dtype='<f4').reshape(48, -1)
    duration = np.fromfile(encoded / 'log_duration.f32', dtype='<f4')
    selected, bounds = predicted_phone_boundaries(ids, duration)
    frames = int(np.ceil(bounds[-1] * 100)) + 1
    x = frame_features(tokens[:, selected], bounds, np.arange(frames) * .01)
    with torch.inference_mode():
        predicted = model.physical(torch.from_numpy(x)[None])
        full = F.interpolate(predicted, size=(frames - 1) * 2 + 1, mode='linear', align_corners=True)
        coded = full[:, :62] * norm.y_std + norm.y_mean
        uv = full[:, 63:64].sigmoid()
        f0 = torch.exp((full[:, 62:63] * norm.f0_std + norm.f0_mean).clamp(4.174387, 6.39693)) * (uv >= .5)
        physical = torch.cat([coded, f0, uv], dim=1)[0].numpy().astype('<f4')
    physical[60:62] = np.clip(physical[60:62], -60, -.001)
    physical.tofile(args.output / 'physical.f32')
    samples = (physical.shape[1] - 1) * 80
    synthesis = cache / 'irodori-realtime-20261114/output16k-host/world_synthesis_cli'
    subprocess.run([str(synthesis), str(args.output / 'physical.f32'), str(args.output / 'wave.f32'),
                    str(samples), '256', '1'], check=True, capture_output=True, timeout=30)
    wave = np.fromfile(args.output / 'wave.f32', dtype='<f4')
    if len(wave) != samples or not np.isfinite(wave).all():
        raise ValueError('Invalid synthesized waveform')
    gain = min(.1 / max(float(np.sqrt(np.mean(wave * wave))), 1e-6),
               .95 / max(float(np.max(np.abs(wave))), 1e-6))
    wavfile.write(args.output / 'candidate.wav', 16000, np.rint(wave * gain * 32767).astype(np.int16))
    report = {'experimental_only': True, 'teacher_timing_used': False, 'teacher_latents_used': False,
              'quality_approved': False, 'device_integrated': False, 'host_seconds': time.monotonic() - started,
              'text': args.text.read_text(), 'text_sha256': sha(args.text),
              'checkpoint_sha256': sha(args.checkpoint), 'encoder': json.loads(result.stdout),
              'audio_seconds': samples / 16000, 'token_ids': ids, 'boundaries_seconds': bounds.tolist(),
              'duration_source': 'Existing SanoTTS FP32 predictor; no target-speaker calibration'}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ['token_ids', 'boundaries_seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--text', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
