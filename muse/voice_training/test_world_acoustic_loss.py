"""ネイティブ復号との一致、損失の単位・時間変化・マスクを検証する。"""
import unittest
import numpy as np
import pyworld as pw
import torch
from world_acoustic_loss import AcousticLoss
from world_model import WorldPredictor
from world_vocoder import SR, FFT_SIZE, SP_DIM, AP_DIM


class AcousticTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        self.loss = AcousticLoss()
        self.model = WorldPredictor(torch.zeros(1, 32, 1), torch.ones(1, 32, 1),
            torch.zeros(1, SP_DIM + AP_DIM, 1), torch.ones(1, SP_DIM + AP_DIM, 1),
            torch.tensor(5.4), torch.tensor(.25))

    def test_matches_native_decoder_on_unseen_coefficients(self):
        rng = np.random.default_rng(113)
        coded = rng.normal(size=(20, SP_DIM)).astype(np.float64)
        coded[:, 0] -= 80
        native = 10 * np.log10(pw.decode_spectral_envelope(coded, SR, FFT_SIZE))
        actual = self.loss.spectrum_db(torch.tensor(coded.T[None], dtype=torch.float32))[0].numpy()
        np.testing.assert_allclose(actual, native, atol=1e-4, rtol=1e-5)

    def test_perfect_components_have_zero_regression_losses(self):
        p = torch.zeros(1, SP_DIM + AP_DIM + 2, 20)
        y = torch.zeros(1, SP_DIM + AP_DIM, 20)
        f0 = torch.full((1, 1, 20), 5.4)
        _, terms = self.loss(p, y, f0, torch.ones_like(f0), self.model)
        for k in ('envelope', 'temporal', 'aperiodicity', 'pitch', 'pitch_delta', 'coefficients'):
            self.assertEqual(float(terms[k]), 0., k)

    def test_wrong_envelope_and_timing_have_gradients(self):
        p = torch.zeros(1, SP_DIM + AP_DIM + 2, 20)
        p[:, 0, 5:10] = 3
        p.requires_grad_()
        y = torch.zeros(1, SP_DIM + AP_DIM, 20)
        f0 = torch.full((1, 1, 20), 5.4)
        loss, terms = self.loss(p, y, f0, torch.ones_like(f0), self.model)
        self.assertGreater(float(terms['envelope'].detach()), 0)
        self.assertGreater(float(terms['temporal'].detach()), 0)
        loss.backward()
        self.assertTrue(torch.isfinite(p.grad).all())
        self.assertGreater(float(p.grad[:, 0].abs().sum()), 0)

    def test_unvoiced_pitch_and_boundaries_are_masked(self):
        p = torch.zeros(1, SP_DIM + AP_DIM + 2, 20)
        y = torch.zeros(1, SP_DIM + AP_DIM, 20)
        f0 = torch.full((1, 1, 20), 5.4); uv = torch.ones_like(f0); uv[:, :, 10:] = 0
        a, _ = self.loss(p, y, f0, uv, self.model)
        f0[:, :, 10:] = -99
        b, _ = self.loss(p, y, f0, uv, self.model)
        torch.testing.assert_close(a, b, rtol=0, atol=0)

    def test_aperiodicity_gate_is_distinct_from_pitch_voicing(self):
        p = torch.zeros(1, SP_DIM + AP_DIM + 2, 20)
        y = torch.zeros(1, SP_DIM + AP_DIM, 20); y[:, SP_DIM:] = -2
        f0 = torch.full((1, 1, 20), 5.4); uv = torch.ones_like(f0)
        _, bad = self.loss(p, y, f0, uv, self.model)
        p[:, SP_DIM:SP_DIM + AP_DIM] = -2
        _, good = self.loss(p, y, f0, uv, self.model)
        self.assertLess(float(good['ap_gate']), float(bad['ap_gate']))


if __name__ == '__main__': unittest.main()
