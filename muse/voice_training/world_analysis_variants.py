"""WORLDの音程推定と非周期成分の有声判定を分けて比較する。"""
import numpy as np
import pyworld as pw

from world_vocoder import SR, FRAME_MS, FFT_SIZE, F0_FLOOR, F0_CEIL, frame_count

CONDITIONS = {
    'current_dio': {'pitch_method': 'dio', 'd4c_threshold': .85},
    'dio_without_ap_gate': {'pitch_method': 'dio', 'd4c_threshold': 0.},
    'harvest': {'pitch_method': 'harvest', 'd4c_threshold': .85},
}


def analyze_variant(audio, condition):
    if condition not in CONDITIONS:
        raise ValueError('未定義の比較条件です')
    y = np.asarray(audio)
    if y.ndim != 1 or len(y) < SR // 10 or not np.isfinite(y).all():
        raise ValueError('0.1秒以上の有限値のモノラル音声が必要です')
    y = np.ascontiguousarray(y, dtype=np.float64)
    config = CONDITIONS[condition]
    estimator = getattr(pw, config['pitch_method'])
    f0, times = estimator(y, SR, f0_floor=F0_FLOOR, f0_ceil=F0_CEIL, frame_period=FRAME_MS)
    # 全条件で同じ精密化を使用する。
    f0 = pw.stonemask(y, f0, times, SR)
    sp = pw.cheaptrick(y, f0, times, SR, f0_floor=F0_FLOOR, fft_size=FFT_SIZE)
    ap = pw.d4c(y, f0, times, SR, threshold=config['d4c_threshold'], fft_size=FFT_SIZE)
    count = frame_count(len(y))

    def extend(values):
        if not 0 <= count - len(values) <= 1:
            raise ValueError('分析フレームの時間軸が一致しません')
        return np.concatenate((values, values[-1:]), axis=0) if count > len(values) else values

    return tuple(map(extend, (f0, sp, ap)))


def voicing_stats(f0, ap):
    f0, ap = np.asarray(f0), np.asarray(ap)
    if (f0.ndim != 1 or len(f0) < 2 or ap.shape != (len(f0), FFT_SIZE // 2 + 1)
            or not np.isfinite(f0).all() or not np.isfinite(ap).all()
            or np.any(f0 < 0) or np.any(ap < 0) or np.any(ap > 1)):
        raise ValueError('音程または非周期成分が不正です')
    voiced = f0 > 0
    fully_aperiodic = np.all(ap >= 1 - 1e-6, axis=1)
    periodic = voiced & ~fully_aperiodic
    return {
        'frames': len(f0), 'f0_voiced_frames': int(voiced.sum()),
        'f0_voicing_transitions': int(np.count_nonzero(voiced[1:] != voiced[:-1])),
        'f0_voiced_but_fully_aperiodic_frames': int((voiced & fully_aperiodic).sum()),
        'periodic_frames': int(periodic.sum()),
        'periodicity_transitions': int(np.count_nonzero(periodic[1:] != periodic[:-1])),
    }
