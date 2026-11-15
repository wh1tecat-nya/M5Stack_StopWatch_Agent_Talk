"""位相を持ち越す励振の試作。音色フィルター・F0予測器は別途学習が必要。"""
from __future__ import annotations
import math

import torch


def continuous_source(f0_hz, voiced, noise, phase_cycles=None, sample_rate=22050, harmonics=16,
                      voiced_noise_ratio=.03):
    """サンプル単位のF0から、周期音・無声音と次チャンクの位相を返す。

    すべて[B, samples]。位相は[B, 1]で、ブロック境界を越えて保持する。
    学習可能な声ではなく、声を作る入力信号のみを生成する。
    noiseは呼出側が供給する。固定した長い雑音を各チャンクへ切り分ければ、
    分割処理と全文処理を同じ条件で比較できる。
    """
    if f0_hz.ndim != 2 or f0_hz.shape != voiced.shape or f0_hz.shape != noise.shape or not f0_hz.numel():
        raise ValueError('F0・有声度・雑音には同形状の空でない[B, samples]が必要です')
    if not f0_hz.is_floating_point() or not voiced.is_floating_point() or not noise.is_floating_point():
        raise ValueError('浮動小数点入力が必要です')
    if voiced.device != f0_hz.device or noise.device != f0_hz.device:
        raise ValueError('入力のデバイスが異なります')
    if not isinstance(harmonics, int) or not 1 <= harmonics <= 32 or sample_rate <= 0 or not 0 <= voiced_noise_ratio <= 1:
        raise ValueError('励振の設定が不正です')
    if any(not torch.isfinite(x).all() for x in (f0_hz, voiced, noise)):
        raise ValueError('入力に非有限値が含まれています')
    if torch.any(f0_hz < 0) or torch.any(f0_hz >= sample_rate / 2) or torch.any((voiced < 0) | (voiced > 1)):
        raise ValueError('F0または有声度が範囲外です')
    if phase_cycles is None:
        phase_cycles = torch.zeros((f0_hz.shape[0], 1), dtype=torch.float64, device=f0_hz.device)
    if phase_cycles.shape != (f0_hz.shape[0], 1) or phase_cycles.device != f0_hz.device or not torch.isfinite(phase_cycles).all():
        raise ValueError('位相状態が不正です')

    # 積分は倍精度で行い、長い発話でも位相を安定させる。将来のMCU版では
    # 固定小数点等による置き換えと速度測定が必要。
    cycles = phase_cycles.to(torch.float64) + torch.cumsum(f0_hz.to(torch.float64) / sample_rate, dim=-1)
    state = torch.remainder(cycles[:, -1:], 1.0)
    periodic = torch.zeros_like(f0_hz)
    for h in range(1, harmonics + 1):
        # ナイキスト近傍で滑らかに減衰させ、その上の倍音をゼロにする。
        weight = ((sample_rate / 2 - f0_hz * h) / (sample_rate * .05)).clamp(0, 1) / h
        oscillator = torch.sin(2 * math.pi * torch.remainder(cycles * h, 1.0)).to(f0_hz.dtype)
        periodic = periodic + weight * oscillator
    normalization = sum(1 / h for h in range(1, harmonics + 1))
    periodic = periodic * voiced * (f0_hz > 0).to(f0_hz.dtype) / normalization
    # 有声音にも小さな非周期成分を残し、息を含む声を表現できるようにする。
    aperiodic = noise * (1 - voiced + voiced * voiced_noise_ratio)
    return periodic, aperiodic, state
