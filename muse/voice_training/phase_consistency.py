"""予測振幅を保持して位相を再推定するGriffin-Lim試作。全文処理専用。"""
import torch
from magnitude_model import FFT, HOP, spectrum, waveform, unit_phase


def refine_phase(magnitude, initial_spectrum, samples, iterations=32):
    """教師音声は受け取らない。現行モデルのスペクトルを初期位相にする。"""
    if (magnitude.ndim != 2 or magnitude.shape != initial_spectrum.shape
            or magnitude.shape[0] != FFT // 2 + 1):
        raise ValueError('同じ形状の[周波数, 時間]スペクトルが必要です')
    if not magnitude.is_floating_point() or not initial_spectrum.is_complex():
        raise ValueError('実数振幅と複素スペクトルが必要です')
    if magnitude.device != initial_spectrum.device or magnitude.dtype != initial_spectrum.real.dtype:
        raise ValueError('デバイスまたは精度が一致しません')
    if (not torch.isfinite(magnitude).all() or (magnitude < 0).any()
            or not torch.isfinite(initial_spectrum).all()):
        raise ValueError('有限のスペクトルと非負の振幅が必要です')
    if (type(samples) is not int or samples < FFT
            or samples // HOP + 1 != magnitude.shape[-1]
            or type(iterations) is not int or not 0 <= iterations <= 64):
        raise ValueError('長さまたは反復回数が不正です')
    angles = unit_phase(initial_spectrum)
    norm = torch.linalg.vector_norm(magnitude).clamp_min(1e-10)
    history = []
    for step in range(iterations + 1):
        projected = magnitude * angles
        y = waveform(projected, samples)
        rebuilt = spectrum(y)
        if not torch.isfinite(y).all():
            raise ValueError('再構成音声に非有限値が含まれます')
        history.append({'iteration': step,
            'magnitude_relative_l2': float(torch.linalg.vector_norm(rebuilt.abs()-magnitude) / norm),
            'complex_consistency_relative_l2': float(torch.linalg.vector_norm(rebuilt-projected) / norm)})
        if step != iterations:
            angles = unit_phase(rebuilt)
    return y, history
