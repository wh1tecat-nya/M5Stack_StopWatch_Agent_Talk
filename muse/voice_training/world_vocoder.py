"""WORLDによる教師依存の分析・再合成。学習済みTTSではない。"""
import math

import numpy as np
import pyworld as pw

SR, FFT_SIZE, FRAME_MS = 22050, 1024, 5.0
SP_DIM, AP_DIM = 60, 2  # WORLDの非周期成分の次元数はサンプリング周波数に依存する。
F0_FLOOR, F0_CEIL = 65.0, 600.0


def frame_count(samples):
    return math.ceil(samples / SR / (FRAME_MS / 1000)) + 1


def analyze(audio):
    y = np.asarray(audio)
    if y.ndim != 1 or len(y) < SR // 10 or not np.isfinite(y).all():
        raise ValueError('分析には0.1秒以上の有限値のモノラル音声が必要です')
    y = np.ascontiguousarray(y, dtype=np.float64)
    f0, times = pw.dio(y, SR, f0_floor=F0_FLOOR, f0_ceil=F0_CEIL, frame_period=FRAME_MS)
    f0 = pw.stonemask(y, f0, times, SR)
    sp = pw.cheaptrick(y, f0, times, SR, f0_floor=F0_FLOOR, fft_size=FFT_SIZE)
    ap = pw.d4c(y, f0, times, SR, fft_size=FFT_SIZE)
    frames = frame_count(len(y))

    def extend(x):
        if not 0 <= frames - len(x) <= 1:
            raise ValueError('分析フレーム数が予想範囲外です')
        return np.concatenate([x, x[-1:]], axis=0) if frames > len(x) else x

    return tuple(map(extend, (f0, sp, ap)))


def encode(f0, sp, ap):
    coded_sp = pw.code_spectral_envelope(np.ascontiguousarray(sp), SR, SP_DIM)
    coded_ap = pw.code_aperiodicity(np.ascontiguousarray(ap), SR)
    if coded_ap.shape[1] != AP_DIM:
        raise ValueError('WORLDの非周期成分の次元数が一致しません')
    features = np.concatenate([coded_sp, coded_ap], axis=1).T.astype(np.float32)
    uv = (f0 > 0).astype(np.float32)[None]
    log_f0 = np.log(np.maximum(f0, F0_FLOOR))[None].astype(np.float32)
    if not np.isfinite(features).all() or not np.isfinite(log_f0).all():
        raise ValueError('音響特徴に非有限値があります')
    return features, log_f0, uv


def extract(audio):
    return encode(*analyze(audio))


def synthesize_full(f0, sp, ap, samples):
    """符号化していない分析パラメータで再合成する診断用経路。"""
    f0, sp, ap = (np.ascontiguousarray(x, dtype=np.float64) for x in (f0, sp, ap))
    if (not isinstance(samples, (int, np.integer)) or samples <= 0 or f0.ndim != 1
            or len(f0) < frame_count(samples) or sp.shape != (len(f0), FFT_SIZE // 2 + 1)
            or ap.shape != sp.shape or not all(np.isfinite(x).all() for x in (f0, sp, ap))
            or np.any(f0 < 0) or np.any(f0 > SR / 2) or np.any(sp <= 0)
            or np.any(ap < 0) or np.any(ap > 1)):
        raise ValueError('合成パラメータが不正です')
    y = pw.synthesize(f0, sp, ap, SR, frame_period=FRAME_MS)
    if len(y) < samples or not np.isfinite(y).all():
        raise ValueError('合成音声が短いか非有限です')
    return y[:samples].astype(np.float32)


def synthesize(features, f0, samples):
    """60次元の音色と2次元の非周期成分を復号して再合成する。"""
    features = np.asarray(features)
    f0 = np.asarray(f0).reshape(-1)
    if features.shape != (SP_DIM + AP_DIM, len(f0)) or not np.isfinite(features).all():
        raise ValueError('符号化パラメータが不正です')
    features = np.ascontiguousarray(features.T, dtype=np.float64)
    sp = pw.decode_spectral_envelope(np.ascontiguousarray(features[:, :SP_DIM]), SR, FFT_SIZE)
    # 正規の有効範囲を外れる推定値を復号器へ渡さない。
    ap = pw.decode_aperiodicity(np.ascontiguousarray(features[:, SP_DIM:].clip(-60, -.001)), SR, FFT_SIZE)
    return synthesize_full(f0, sp, np.clip(ap, 1e-6, 1.), samples)
