"""振幅補正の分離、位相固定、時間対応、推論の教師非依存を検証する。"""
import inspect
import random
import unittest
import torch
from torch import nn
import magnitude_model as m


class FakeBase(nn.Module):
    def __init__(self):
        super().__init__()
        self.gain = nn.Parameter(torch.tensor(.1))

    def forward(self, z):
        t = torch.arange(z.shape[-1] * m.HOP, device=z.device) / m.SR
        return (self.gain * torch.sin(2 * torch.pi * 237 * t))[None].expand(z.shape[0], -1)


class MagnitudeTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(21)
        self.head = m.MagnitudeCorrection(torch.zeros(1, 32, 1), torch.ones(1, 32, 1))

    def test_zero_correction_preserves_magnitude(self):
        z = torch.randn(2, 32, 20)
        a = torch.rand(2, 513, 20) * 12
        torch.testing.assert_close(self.head(z, a), a, rtol=2e-6, atol=2e-6)

    def test_centered_stft_frame_counts_and_roundtrip(self):
        for n in (4096, 4001):
            frames = (n + m.HOP - 1) // m.HOP
            y = torch.randn(1, n) * .1
            s = m.spectrum(y)
            z = torch.randn(1, 32, frames)
            self.assertEqual(m.match_frames(z, s.shape[-1]).shape[-1], s.shape[-1])
            torch.testing.assert_close(m.waveform(s, n), y, atol=1e-6, rtol=1e-5)
        with self.assertRaises(ValueError): m.match_frames(z, 30)

    def test_inference_has_no_teacher_argument_and_identity_start(self):
        self.assertEqual(list(inspect.signature(m.FrozenPhaseReconstruction.forward).parameters),
                         ['self', 'z', 'samples'])
        model = m.FrozenPhaseReconstruction(FakeBase(), self.head)
        z = torch.randn(1, 32, 30)
        for samples in (30 * m.HOP, 30 * m.HOP - 113):
            torch.testing.assert_close(model(z, samples), model.base(z)[:, :samples], atol=1e-6, rtol=1e-5)

    def test_training_updates_correction_only(self):
        model = m.FrozenPhaseReconstruction(FakeBase(), self.head).train()
        z = torch.randn(1, 32, 24)
        base_before = model.base.gain.clone()
        out = model(z, 24 * m.HOP)
        loss = (out - model.base(z) * .7).square().mean()
        loss.backward()
        grads = [p.grad for p in self.head.parameters() if p.grad is not None]
        self.assertTrue(all(torch.isfinite(g).all() for g in grads))
        self.assertGreater(sum(float(g.abs().sum()) for g in grads), 0)
        torch.optim.SGD(self.head.parameters(), lr=.1).step()
        self.assertIsNone(model.base.gain.grad)
        self.assertFalse(model.base.training)
        self.assertTrue(torch.equal(base_before, model.base.gain))

    def test_segments_keep_all_three_time_axes(self):
        item = {k: torch.arange(200).float().repeat(c, 1)
                for k, c in [('z', 32), ('base', 513), ('target', 513)]}
        z, a, t = m.batch_segments([item], random.Random(2), 2, 'cpu')
        self.assertTrue(torch.equal(z[:, 0], a[:, 0]))
        self.assertTrue(torch.equal(a, t))
        self.assertEqual(z.shape[-1], m.SEGMENT + 2 * m.CONTEXT)

    def test_silence_phase_and_loss_finite(self):
        zero = torch.zeros(1, 513, 20)
        self.assertTrue(torch.equal(m.unit_phase(zero.to(torch.complex64)), torch.ones_like(zero)))
        self.assertEqual(float(m.magnitude_loss(zero, zero)), 0.)
        torch.testing.assert_close(self.head(torch.zeros(1, 32, 20), zero), zero)

    def test_context_crop_matches_full_forward(self):
        nn.init.normal_(self.head.output.weight, std=.001)
        z, a = torch.randn(1, 32, 200), torch.rand(1, 513, 200)
        full = self.head(z, a)
        start, width = 30, m.SEGMENT + 2 * m.CONTEXT
        cropped = self.head(z[:, :, start:start + width], a[:, :, start:start + width])
        torch.testing.assert_close(cropped[:, :, m.CONTEXT:-m.CONTEXT],
                                   full[:, :, start + m.CONTEXT:start + width - m.CONTEXT])

    def test_phase_is_preserved_before_inverse_transform(self):
        s = torch.complex(torch.randn(1, 513, 20), torch.randn(1, 513, 20))
        corrected = torch.rand_like(s.real).clamp_min(.01) * m.unit_phase(s)
        torch.testing.assert_close(m.unit_phase(corrected), m.unit_phase(s))


if __name__ == '__main__':
    unittest.main()
