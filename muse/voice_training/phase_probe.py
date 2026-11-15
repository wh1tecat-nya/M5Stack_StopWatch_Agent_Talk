#!/usr/bin/env python3
"""振幅と位相を入れ替える原因切り分け。教師成分を使うため採用候補ではない。"""
import argparse
import hashlib
import json
from pathlib import Path
import wave

import numpy as np
import torch

from diagnose_decoder import compare
from compare_band_results import save_previews

SR, FFT, HOP = 22050, 1024, 256


def spectrum(y):
    return torch.stft(y, FFT, HOP, window=torch.hann_window(FFT, dtype=y.dtype),
                      center=True, return_complex=True)


def waveform(s, length):
    return torch.istft(s, FFT, HOP, window=torch.hann_window(FFT, dtype=s.real.dtype),
                       center=True, length=length)


def unit_phase(s):
    magnitude = s.abs()
    return torch.where(magnitude > 1e-10, s / magnitude.clamp_min(1e-10), torch.ones_like(s))


def separate(reference, candidate):
    if reference.ndim != 1 or reference.shape != candidate.shape or len(reference) < FFT:
        raise ValueError('同じ長さの1次元音声が必要です')
    if not torch.isfinite(reference).all() or not torch.isfinite(candidate).all():
        raise ValueError('非有限値を含む音声は比較できません')
    ref, gen = spectrum(reference), spectrum(candidate)
    return {
        'reference': reference,
        'candidate': candidate,
        'candidate_magnitude_teacher_phase': waveform(gen.abs() * unit_phase(ref), len(reference)),
        'teacher_magnitude_candidate_phase': waveform(ref.abs() * unit_phase(gen), len(reference)),
        'reference_roundtrip': waveform(ref, len(reference)),
        'candidate_roundtrip': waveform(gen, len(candidate)),
    }


def load(path):
    with wave.open(str(path)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (SR, 1, 2):
            raise ValueError('22,050 Hz・mono・PCM16が必要です')
        return torch.from_numpy(np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').copy()).float() / 32768


def run(reference_path, candidate_path, output):
    torch.set_num_threads(2)
    reference, candidate = load(reference_path), load(candidate_path)
    audios = separate(reference, candidate)
    controls = {}
    for name in ('reference', 'candidate'):
        error = torch.linalg.vector_norm(audios[name] - audios[name + '_roundtrip']) / torch.linalg.vector_norm(audios[name]).clamp_min(1e-10)
        if error > 1e-5:
            raise RuntimeError('STFT往復の誤差が大きすぎます')
        controls[name + '_roundtrip_relative_l2'] = float(error)
    report = {
        'kind': 'diagnostic_teacher_component_swap_only',
        'deployable': False, 'trained_model_changed': False, 'perceptual_quality_verified': False,
        'sample_rate': SR, 'fft': FFT, 'hop': HOP,
        'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (reference_path, candidate_path)},
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'controls': controls,
        'caveat': '教師の位相または振幅を借りる診断です。実装可能な改善版ではありません。混合スペクトルは一般に非整合で、iSTFT後には両成分が変わり得るため、単独の原因や改善幅を確定できません。',
        'comparisons': {},
    }
    listen = {k: v.numpy() for k, v in audios.items() if not k.endswith('_roundtrip')}
    for name, audio in listen.items():
        if name != 'reference':
            report['comparisons'][name] = compare(reference.numpy(), audio)
    output.mkdir(parents=True, exist_ok=False)
    save_previews(output / 'listening', listen, {'candidate': 2000})
    with (output / 'report.json').open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'output': str(output), 'controls': controls,
                     'diagnostic_only': True}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.reference, args.candidate, args.output)
