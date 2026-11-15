"""時間軸・基本周波数・無音・入力安全性と、未学習予測器の計算を検査する。"""
import unittest

import numpy as np
import torch
from world_vocoder import SR, FRAME_MS, SP_DIM, AP_DIM, analyze, encode, synthesize, synthesize_full, frame_count
from world_model import WorldPredictor, objective, CONTEXT


class WorldVocoderTests(unittest.TestCase):
    def setUp(self):
        t = np.arange(SR) / SR
        self.audio = sum(.1 / n * np.sin(2 * np.pi * 220 * n * t) for n in range(1, 12))

    def test_known_pitch_and_full_resolution_synthesis(self):
        f0, sp, ap = analyze(self.audio)
        self.assertLess(abs(np.median(f0[f0 > 0]) - 220), 2)
        self.assertGreater(np.mean(f0 > 0), .9)
        out = synthesize_full(f0, sp, ap, len(self.audio))
        self.assertEqual(out.shape, self.audio.shape)
        self.assertTrue(np.isfinite(out).all())
        self.assertGreater(np.std(out), .01)

    def test_fractional_samples_per_frame_and_compact_encoding(self):
        # 22,050Hz・5msは110.25サンプル。整数hopへの丸めによる累積ずれを防ぐ。
        for samples in (SR, SR - 17):
            f0, sp, ap = analyze(self.audio[:samples])
            features, log_f0, uv = encode(f0, sp, ap)
            self.assertEqual(features.shape, (SP_DIM + AP_DIM, frame_count(samples)))
            self.assertEqual(log_f0.shape, uv.shape)
            self.assertGreaterEqual((len(f0) - 1) * FRAME_MS / 1000, samples / SR)
            out = synthesize(features, f0, samples)
            self.assertEqual(len(out), samples)
            self.assertTrue(np.isfinite(out).all())

    def test_silence_stays_inaudible(self):
        f0, sp, ap = analyze(np.zeros(SR))
        self.assertFalse(np.any(f0))
        features, _, _ = encode(f0, sp, ap)
        for out in (synthesize_full(f0, sp, ap, SR), synthesize(features, f0, SR)):
            self.assertTrue(np.isfinite(out).all())
            self.assertLess(np.max(np.abs(out)), 1e-6)

    def test_invalid_input_is_rejected_before_native_synthesis(self):
        for audio in (np.zeros(10), np.zeros((SR, 2)), self.audio * np.nan):
            with self.assertRaises(ValueError): analyze(audio)
        f0, sp, ap = analyze(self.audio)
        for args in ((-np.ones_like(f0), sp, ap, SR), (f0, sp * np.nan, ap, SR),
                     (f0, -sp, ap, SR), (f0, sp, ap * 0 + 2, SR),
                     (f0, sp[:-1], ap, SR), (f0, sp, ap, 2 * SR)):
            with self.assertRaises(ValueError): synthesize_full(*args)


class WorldPredictorTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(112)
        self.model = WorldPredictor(torch.zeros(1, 32, 1), torch.ones(1, 32, 1),
            torch.zeros(1, SP_DIM + AP_DIM, 1), torch.ones(1, SP_DIM + AP_DIM, 1),
            torch.tensor(np.log(220.)), torch.tensor(.2))

    def test_context_crop_matches_full_inference(self):
        z = torch.randn(1, 32, 160)
        full = self.model(z)
        part = self.model(z[:, :, 32-CONTEXT:128+CONTEXT])[:, :, CONTEXT:-CONTEXT]
        torch.testing.assert_close(full[:, :, 32:128], part)

    def test_unvoiced_pitch_labels_do_not_affect_loss(self):
        prediction = self.model(torch.randn(2, 32, 60))
        features = torch.zeros(2, SP_DIM + AP_DIM, 60)
        voiced = torch.zeros(2, 1, 60)
        a, terms = objective(prediction, features, torch.zeros_like(voiced), voiced, self.model)
        b, _ = objective(prediction, features, voiced + 100, voiced, self.model)
        torch.testing.assert_close(a, b)
        self.assertEqual(float(terms['pitch'].detach()), 0.)
        a.backward()
        self.assertTrue(all(p.grad is not None and torch.isfinite(p.grad).all() for p in self.model.parameters()))

    def test_physical_output_has_valid_pitch_range(self):
        with torch.no_grad():
            self.model.output.weight.zero_()
            self.model.output.bias.zero_()
            self.model.output.bias[-1] = 10
            self.model.output.bias[-2] = 100
        features, f0, uv = self.model.physical(torch.zeros(1, 32, 40))
        self.assertEqual(features.shape, (1, SP_DIM + AP_DIM, 40))
        self.assertTrue(torch.all((f0 >= 65) & (f0 <= 600.001)))
        self.assertTrue(torch.all((uv >= 0) & (uv <= 1)))


if __name__ == '__main__':
    unittest.main()
