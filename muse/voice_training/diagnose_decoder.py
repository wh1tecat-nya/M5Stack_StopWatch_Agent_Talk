#!/usr/bin/env python3
"""教師と再構成の帯域エネルギー、周期的な揺れ、音程を比較する。

音程は単純な自己相関推定による補助指標。聴感上の品質や原因の確定には使わない。
"""
import argparse
import hashlib
import json
from pathlib import Path
import wave

import numpy as np
from scipy import signal

SR, HOP = 22050, 256
BANDS = [(80, 500), (500, 1500), (1500, 3000), (3000, 6000), (6000, 10000)]


def read_wave(path):
    with wave.open(str(path)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (SR, 1, 2):
            raise ValueError('比較音声は22,050Hz・mono・PCM16が必要です')
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(float) / 32768


def pitch_track(y):
    """80〜600Hz。正規化自己相関0.8以上だけを有声音として扱う。"""
    width = 1536
    filtered = signal.sosfiltfilt(signal.butter(4, 1500, fs=SR, output='sos'), y)
    frames = np.lib.stride_tricks.sliding_window_view(filtered, width)[::HOP].copy()
    frames -= frames.mean(axis=1, keepdims=True)
    frames *= np.hanning(width)
    corr = np.fft.irfft(np.abs(np.fft.rfft(frames, n=4096, axis=1)) ** 2, n=4096, axis=1)
    # 窓による相関減衰を補正する。
    window_corr = signal.correlate(np.hanning(width), np.hanning(width), mode='full')[width-1:]
    corr = corr[:, :width] / np.maximum(window_corr, 1e-12)
    corr /= np.maximum(corr[:, :1], 1e-12)
    lo, hi = int(SR / 600), int(SR / 80)
    pitches, strengths = [], []
    for row, frame in zip(corr, frames):
        peaks, _ = signal.find_peaks(row[lo:hi])
        peaks += lo
        if not len(peaks) or np.sqrt(np.mean(frame ** 2)) < .004:
            pitches.append(np.nan); strengths.append(0.); continue
        strongest = np.max(row[peaks])
        # 高い倍周期への誤追従を抑えるため、最大値に近い最短周期を使う。
        lag = peaks[np.flatnonzero(row[peaks] >= strongest * .95)[0]]
        strength = row[lag]
        correction = .5 * (row[lag-1] - row[lag+1]) / min(row[lag-1] - 2*row[lag] + row[lag+1], -1e-12)
        pitches.append(SR / (lag + np.clip(correction, -.5, .5)) if strength >= .8 else np.nan)
        strengths.append(float(strength))
    return np.asarray(pitches), np.asarray(strengths)


def measurements(y):
    frequencies, power = signal.welch(y, SR, nperseg=4096, noverlap=3072)
    energies = [float(np.sum(power[(frequencies >= lo) & (frequencies < hi)])) for lo, hi in BANDS]
    high = signal.sosfilt(signal.butter(5, [3000, 9000], fs=SR, btype='bandpass', output='sos'), y)
    envelope = signal.sosfilt(signal.butter(4, 300, fs=SR, output='sos'), np.abs(signal.hilbert(high)))
    f, p = signal.welch(envelope, SR, nperseg=8192)
    center = SR / HOP
    prominence = 10 * np.log10(max(np.max(p[np.abs(f-center) < 3]), 1e-20) /
                              max(np.median(p[(np.abs(f-center) > 4) & (np.abs(f-center) < 15)]), 1e-20))
    pitch, _ = pitch_track(y)
    valid = pitch[np.isfinite(pitch)]
    grid_distance = np.abs(valid - np.rint(valid / (SR/HOP)) * (SR/HOP))
    return {'rms': float(np.sqrt(np.mean(y ** 2))), 'band_energies': energies,
            'envelope_86hz_prominence_db': float(prominence),
            'pitch_median_hz': float(np.median(valid)) if len(valid) else None,
            'pitch_near_hop_harmonic_fraction_5hz': float(np.mean(grid_distance < 5)) if len(valid) else None,
            'pitch_hop_grid_distance_median_hz': float(np.median(grid_distance)) if len(valid) else None,
            'voiced_frames': int(len(valid)), 'frames': len(pitch)}, pitch


def compare(reference, reconstruction):
    if len(reference) != len(reconstruction):
        raise ValueError('比較音声の長さが異なります')
    ref, rp = measurements(reference)
    gen, gp = measurements(reconstruction)
    common = np.isfinite(rp) & np.isfinite(gp)
    threshold = float(np.median(rp[np.isfinite(rp)])) if np.isfinite(rp).any() else 0.
    pitch = {'method': 'window-corrected normalized autocorrelation; not a ground-truth F0 label',
             'high_register_threshold_hz': threshold, 'common_voiced_frames': int(common.sum()),
             'reference_voiced_missing_fraction': float(np.sum(np.isfinite(rp) & ~np.isfinite(gp)) / max(np.isfinite(rp).sum(), 1))}
    for key, mask in [('lower_register', common & (rp < threshold)), ('higher_register', common & (rp >= threshold))]:
        cents = 1200 * np.abs(np.log2(gp[mask] / rp[mask]))
        pitch[key] = {'frames': int(mask.sum()),
                      'median_absolute_cents': float(np.median(cents)) if len(cents) else None,
                      'over_100_cents_fraction': float(np.mean(cents > 100)) if len(cents) else None}
    return {'reference': ref, 'reconstruction': gen,
            'band_gain_db': {f'{lo}-{hi}': float(10*np.log10(max(g, 1e-20)/max(r, 1e-20)))
                             for (lo, hi), r, g in zip(BANDS, ref['band_energies'], gen['band_energies'])},
            'pitch_comparison': pitch,
            'caveat': '86Hz値は周囲の変調成分に対する突出量。聴感・因果・音程正解を証明しない。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outputs', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    results = {}
    for folder in sorted(args.outputs.glob('mf07-*')):
        if not (folder / 'reference.wav').exists(): continue
        paths = [folder / name for name in ('reference.wav', 'reconstruction.wav')]
        results[folder.name] = {**compare(*[read_wave(p) for p in paths]),
                               'input_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    if not results: raise ValueError('比較対象がありません')
    report = {'analysis_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'samples': results}
    with args.report.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))
