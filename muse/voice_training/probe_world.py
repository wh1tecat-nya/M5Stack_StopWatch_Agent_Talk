#!/usr/bin/env python3
"""学習前にWORLDの分析・合成と特徴圧縮の品質を分けて比較する。"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import wave

import numpy as np
import pyworld as pw
from scipy.signal import resample_poly

from compare_band_results import save_previews
from world_vocoder import SR, FRAME_MS, SP_DIM, AP_DIM, analyze, encode, synthesize, synthesize_full

TEACHER_SHA = '14477312d3543f1b52b77b73f73b9fdd3ea5c00415b7965ef096ad0857de578c'
ACCEPTED_SHA = '5bcfe7d28d56dce7f014ab0f822864f3ef67e1024ceff282b1ee031b92452b89'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_pcm(path, sr):
    with wave.open(str(path)) as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (sr, 1, 2):
            raise ValueError('入力音声の形式が異なります')
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768


def run(args):
    if digest(args.teacher) != TEACHER_SHA or digest(args.accepted) != ACCEPTED_SHA:
        raise ValueError('MF07の元音声、またはユーザー確認済みの基準Bと一致しません')
    original = resample_poly(read_pcm(args.teacher, 48000), 147, 320).astype(np.float32)
    accepted = read_pcm(args.accepted, SR)
    if original.shape != accepted.shape or len(original) != 282240:
        raise ValueError('基準Bと同じ12.8秒の返答ではありません')
    gain = np.dot(accepted.astype(float), original) / np.dot(original.astype(float), original)
    reference_error = float(np.linalg.norm(accepted - gain * original) / np.linalg.norm(accepted))
    if reference_error > 2e-4:
        raise ValueError('基準Bとの差がPCM丸め誤差を超えています')
    started = time.monotonic()
    f0, sp, ap = analyze(original)
    features, log_f0, uv = encode(f0, sp, ap)
    full = synthesize_full(f0, sp, ap, len(original))
    compact = synthesize(features, f0, len(original))
    elapsed = time.monotonic() - started
    save_previews(args.output, {'accepted_reference_B': accepted, 'world_full_parameters': full,
                               'world_compact_parameters': compact}, {}, pcm_scale=32768)
    np.savez_compressed(args.output / 'analysis_parameters.npz', f0=f0, sp=sp, ap=ap,
                        features=features, log_f0=log_f0, uv=uv)
    stats = {}
    for name in ('accepted_reference_B', 'world_full_parameters', 'world_compact_parameters'):
        path = args.output / (name + '.wav')
        audio = read_pcm(path, SR)
        pcm = np.rint(audio * 32768).astype(np.int32)
        stats[name] = {'sha256': digest(path), 'samples': len(audio), 'duration': len(audio) / SR,
                       'rms': float(np.sqrt(np.mean(audio.astype(float) ** 2))),
                       'peak': float(np.max(np.abs(audio))),
                       'pcm_rails': int(np.count_nonzero((pcm == -32768) | (pcm == 32767)))}
        if len(audio) != len(original) or stats[name]['pcm_rails'] or not np.isfinite(audio).all():
            raise ValueError('試聴音声の検査に失敗しました')
    if np.ptp([v['rms'] for v in stats.values()]) > 5e-7:
        raise ValueError('試聴音声の平均RMSが一致しません')
    voiced = f0[f0 > 0]
    report = {'kind': 'teacher_dependent_WORLD_analysis_synthesis_probe', 'pyworld': pw.__version__,
              'source_sha256': TEACHER_SHA, 'accepted_B_sha256': ACCEPTED_SHA,
              'code_sha256': digest(__file__), 'vocoder_code_sha256': digest(Path(__file__).with_name('world_vocoder.py')),
              'frame_period_ms': FRAME_MS, 'spectral_dimensions_compact': SP_DIM,
              'aperiodic_dimensions_compact': AP_DIM, 'frames': len(f0),
              'voiced_fraction': float(np.mean(f0 > 0)),
              'f0_percentiles': np.percentile(voiced, [0, 5, 50, 95, 100]).tolist(),
              'reference_gain': float(gain), 'reference_relative_pcm_error': reference_error,
              'analysis_and_two_syntheses_seconds_mac': elapsed,
              'files': stats, 'validation_passed': True,
              'both_WORLD_clips_use_teacher_parameters': True, 'new_training_performed': False,
              'quality_approved': False, 'text_to_speech_ready': False, 'firmware_changed': False,
              'C152_real_time_verified': False,
              'limitations': ['F0はDIOとStoneMaskの推定値であり、声の高さの正解ラベルではありません。',
                              '解析・合成方式と圧縮の切り分け用です。改善済みの小型モデル出力ではありません。',
                              'CPU全文処理の実測は実機性能を保証しません。',
                              '基準Bの同一文章だけの比較です。音量は全文RMS一致でありLUFS一致ではありません。']}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--teacher', type=Path, required=True)
    p.add_argument('--accepted', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    run(p.parse_args())
