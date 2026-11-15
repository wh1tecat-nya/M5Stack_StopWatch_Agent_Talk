"""比較の往復精度、無音、入力拒否、振幅・位相の取り違えを確認する。"""
import unittest
import torch
import phase_probe as probe


class PhaseProbeTests(unittest.TestCase):
    def test_identical_input_is_preserved_by_all_branches(self):
        torch.manual_seed(7)
        y = torch.randn(8192) * .1
        for output in probe.separate(y, y).values():
            self.assertTrue(torch.allclose(y, output, atol=1e-6))

    def test_silence_remains_finite(self):
        y = torch.zeros(4096)
        for output in probe.separate(y, y).values():
            self.assertTrue(torch.equal(y, output))

    def test_swap_labels_match_independent_stft_construction(self):
        torch.manual_seed(3)
        ref, gen = torch.randn(4096) * .1, torch.randn(4096) * .1
        r, g = probe.spectrum(ref), probe.spectrum(gen)
        outputs = probe.separate(ref, gen)
        mag_only = probe.waveform(torch.polar(g.abs(), r.angle()), len(ref))
        phase_only = probe.waveform(torch.polar(r.abs(), g.angle()), len(ref))
        self.assertTrue(torch.allclose(outputs['candidate_magnitude_teacher_phase'], mag_only, atol=1e-6))
        self.assertTrue(torch.allclose(outputs['teacher_magnitude_candidate_phase'], phase_only, atol=1e-6))
        self.assertFalse(torch.allclose(mag_only, phase_only))

    def test_invalid_inputs_are_rejected(self):
        for ref, gen in [(torch.zeros(1024), torch.zeros(1025)),
                         (torch.zeros(20), torch.zeros(20)),
                         (torch.zeros(2048), torch.full((2048,), float('nan')))]:
            with self.assertRaises(ValueError): probe.separate(ref, gen)


if __name__ == '__main__':
    unittest.main()
