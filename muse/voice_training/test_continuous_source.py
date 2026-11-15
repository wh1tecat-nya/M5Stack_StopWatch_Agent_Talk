"""波形のチャンク連続性、非整数音程、無声音、勾配と倍音帯域を確認する。"""
import math
import unittest
import torch
from continuous_source import continuous_source


class ContinuousSourceTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_phase_and_noise(self):
        torch.manual_seed(41)
        f0 = torch.linspace(170, 420, 10007)[None]
        uv = torch.linspace(0, 1, 10007)[None]
        noise = torch.randn_like(f0) * .1
        periodic, aperiodic, state = continuous_source(f0, uv, noise)
        p, a, phase = [], [], None
        borders = [0, 511, 1024, 4097, 9000, 10007]
        for start, end in zip(borders[:-1], borders[1:]):
            x, y, phase = continuous_source(f0[:, start:end], uv[:, start:end], noise[:, start:end], phase)
            p.append(x); a.append(y)
        self.assertTrue(torch.allclose(periodic, torch.cat(p, -1), atol=1e-6))
        self.assertTrue(torch.equal(aperiodic, torch.cat(a, -1)))
        self.assertTrue(torch.allclose(state, phase, atol=1e-10))

    def test_constant_off_grid_pitch_does_not_repeat_each_hop(self):
        f0 = torch.full((1, 22050), 237.)
        y, _, _ = continuous_source(f0, torch.ones_like(f0), torch.zeros_like(f0), harmonics=1)
        spectrum = torch.fft.rfft(y[0]).abs()
        self.assertEqual(int(spectrum.argmax()), 237)
        self.assertGreater(float((y[:, 256:] - y[:, :-256]).abs().mean()), .1)

    def test_unvoiced_sibilant_source_has_no_periodic_component(self):
        torch.manual_seed(9)
        f0 = torch.full((1, 4096), 300.)
        noise = torch.randn_like(f0) * .1
        p, a, _ = continuous_source(f0, torch.zeros_like(f0), noise)
        self.assertEqual(float(p.abs().max()), 0)
        self.assertTrue(torch.equal(a, noise))

    def test_harmonics_above_nyquist_are_removed(self):
        f0 = torch.full((1, 4096), 8000.)
        p, _, _ = continuous_source(f0, torch.ones_like(f0), torch.zeros_like(f0), harmonics=4)
        t = torch.arange(1, 4097, dtype=torch.float64) / 22050
        expected = torch.sin(2 * math.pi * 8000 * t).float() / sum(1/h for h in range(1, 5))
        self.assertTrue(torch.allclose(p[0], expected, atol=1e-6))

    def test_voiced_source_keeps_a_small_aperiodic_component(self):
        f0 = torch.full((1, 1024), 250.)
        noise = torch.full_like(f0, .1)
        _, a, _ = continuous_source(f0, torch.ones_like(f0), noise)
        self.assertTrue(torch.allclose(a, noise * .03))

    def test_frequency_gradient_is_finite(self):
        f0 = torch.full((1, 1024), 230., requires_grad=True)
        p, _, _ = continuous_source(f0, torch.ones_like(f0), torch.zeros_like(f0))
        (p.square().mean()).backward()
        self.assertTrue(torch.isfinite(f0.grad).all())
        self.assertGreater(float(f0.grad.abs().sum()), 0)

    def test_invalid_inputs_are_rejected(self):
        for f0 in [torch.zeros(20), torch.zeros(1, 0), torch.full((1, 1024), float('nan')), torch.full((1, 1024), 22050.)]:
            with self.assertRaises(ValueError):
                continuous_source(f0, torch.ones_like(f0), torch.zeros_like(f0))


if __name__ == '__main__':
    unittest.main()
