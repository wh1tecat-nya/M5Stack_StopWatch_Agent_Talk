"""WORLDで復号した音色と時間変化を評価する、学習専用の損失。"""
import numpy as np
import pyworld as pw
import torch
from torch import nn
import torch.nn.functional as F

from world_vocoder import SR, FFT_SIZE, SP_DIM


class AcousticLoss(nn.Module):
    def __init__(self):
        super().__init__()
        zero = np.zeros((1, SP_DIM), dtype=np.float64)
        bias = np.log(pw.decode_spectral_envelope(zero, SR, FFT_SIZE))[0]
        basis = np.log(pw.decode_spectral_envelope(np.eye(SP_DIM, dtype=np.float64), SR, FFT_SIZE)) - bias
        self.register_buffer('basis_db', torch.tensor(basis * (10 / np.log(10)), dtype=torch.float32))
        self.register_buffer('bias_db', torch.tensor(bias * (10 / np.log(10)), dtype=torch.float32))
        frequency = np.arange(FFT_SIZE // 2 + 1) * SR / FFT_SIZE
        # 低域が周波数ビン数だけで過小評価されないよう4帯域を等重みとする。
        weights = np.zeros_like(frequency)
        for lo, hi in ((0, 500), (500, 2000), (2000, 4000), (4000, SR / 2 + 1)):
            mask = (frequency >= lo) & (frequency < hi)
            weights[mask] = .25 / mask.sum()
        self.register_buffer('weights', torch.tensor(weights, dtype=torch.float32))

    def spectrum_db(self, coded):
        return torch.matmul(coded.transpose(1, 2), self.basis_db) + self.bias_db

    def band_mean(self, values):
        return (values * self.weights).sum(-1).mean()

    def forward(self, prediction, features, log_f0, voiced, model):
        physical = prediction[:, :-2] * model.y_std + model.y_mean
        predicted_sp = self.spectrum_db(physical[:, :SP_DIM])
        target_sp = self.spectrum_db(features[:, :SP_DIM])
        error_db = predicted_sp - target_sp
        envelope = self.band_mean(F.smooth_l1_loss(error_db / 6, torch.zeros_like(error_db), reduction='none'))
        delta = error_db[:, 1:] - error_db[:, :-1]
        temporal = self.band_mean(F.smooth_l1_loss(delta / 3, torch.zeros_like(delta), reduction='none'))
        ap_pred, ap_target = physical[:, SP_DIM:], features[:, SP_DIM:]
        aperiodicity = F.smooth_l1_loss(ap_pred / 3, ap_target / 3)
        # WORLDのAP復号器は平均-0.5dBで処理を切り替える。F0の有声判定とは別に学習する。
        target_gate = (ap_target.clamp(-60, -.001).mean(1) <= -.5).float()
        gate_logits = (-.5 - ap_pred.mean(1)) / .25
        ap_gate = F.binary_cross_entropy_with_logits(gate_logits, target_gate)
        predicted_log_f0 = prediction[:, -2:-1] * model.f0_std + model.f0_mean
        cents_error = (predicted_log_f0 - log_f0) * (1200 / np.log(2))
        pitch = (F.smooth_l1_loss(cents_error / 100, torch.zeros_like(cents_error), reduction='none') * voiced).sum() / voiced.sum().clamp_min(1)
        consecutive = voiced[:, :, 1:] * voiced[:, :, :-1]
        pitch_change = cents_error[:, :, 1:] - cents_error[:, :, :-1]
        pitch_delta = (F.smooth_l1_loss(pitch_change / 50, torch.zeros_like(pitch_change), reduction='none') * consecutive).sum() / consecutive.sum().clamp_min(1)
        voicing = F.binary_cross_entropy_with_logits(prediction[:, -1:], voiced)
        coefficients = F.smooth_l1_loss(prediction[:, :-2], (features - model.y_mean) / model.y_std)
        total = envelope + .2 * temporal + .25 * aperiodicity + .15 * ap_gate + .25 * pitch + .05 * pitch_delta + .25 * voicing + .05 * coefficients
        terms = dict(envelope=envelope, temporal=temporal, aperiodicity=aperiodicity, ap_gate=ap_gate,
                     pitch=pitch, pitch_delta=pitch_delta, voicing=voicing, coefficients=coefficients)
        return total, terms

    @torch.no_grad()
    def metrics(self, prediction, features, log_f0, voiced, model):
        physical = prediction[:, :-2] * model.y_std + model.y_mean
        error_db = self.spectrum_db(physical[:, :SP_DIM]) - self.spectrum_db(features[:, :SP_DIM])
        pg = physical[:, SP_DIM:].clamp(-60, -.001).mean(1) <= -.5
        tg = features[:, SP_DIM:].clamp(-60, -.001).mean(1) <= -.5
        pv = prediction[:, -1:] >= 0
        tv = voiced > 0
        both = pv & tv
        cents = (prediction[:, -2:-1] * model.f0_std + model.f0_mean - log_f0).abs() * (1200 / np.log(2))
        return {'envelope_mae_db': float(self.band_mean(error_db.abs())),
                'envelope_step_error_db': float(self.band_mean((error_db[:, 1:] - error_db[:, :-1]).abs())),
                'ap_gate_error': float((pg != tg).float().mean()),
                'uv_error': float((pv != tv).float().mean()),
                'pitch_mae_cents_joint_voiced': float(cents[both].mean()) if both.any() else None}
