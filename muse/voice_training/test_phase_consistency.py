"""位相再推定が正解不要で動き、既に整合する音声を保つことを確認する。"""
import inspect
import tempfile
import unittest
import wave
from pathlib import Path
import numpy as np
import torch
from magnitude_model import spectrum, waveform, SR
from phase_consistency import refine_phase
from compare_band_results import save_previews


class PhaseConsistencyTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(12)
        t = torch.arange(8192) / SR
        self.y = .1 * torch.sin(2*torch.pi*237*t) + .03 * torch.sin(2*torch.pi*474*t)
        self.s = spectrum(self.y)

    def test_consistent_input_is_preserved(self):
        for n in (8192, 8077):
            y, s = self.y[:n], spectrum(self.y[:n])
            actual, _ = refine_phase(s.abs(), s, n, 8)
            torch.testing.assert_close(actual, y, atol=1e-6, rtol=1e-4)

    def test_zero_iterations_matches_existing_reconstruction(self):
        a = self.s.abs() * .7
        y, history = refine_phase(a, self.s, len(self.y), 0)
        torch.testing.assert_close(y, waveform(self.s*.7, len(self.y)), atol=1e-6, rtol=1e-4)
        self.assertEqual(len(history), 1)

    def test_inconsistent_phase_moves_toward_target_magnitude(self):
        angle = torch.rand_like(self.s.real) * (2*torch.pi)
        init = torch.polar(torch.ones_like(angle), angle)
        a = self.s.abs()
        a_before, init_before = a.clone(), init.clone()
        y, h = refine_phase(a, init, len(self.y), 32)
        self.assertLess(h[-1]['magnitude_relative_l2'], h[0]['magnitude_relative_l2'] * .5)
        self.assertTrue(torch.equal(a, a_before) and torch.equal(init, init_before))
        self.assertEqual(y.shape, self.y.shape)
        self.assertEqual(list(inspect.signature(refine_phase).parameters),
                         ['magnitude', 'initial_spectrum', 'samples', 'iterations'])

    def test_silence_is_finite_and_silent(self):
        y, h = refine_phase(self.s.abs()*0, self.s*0, len(self.y), 4)
        self.assertTrue(torch.equal(y, torch.zeros_like(y)))
        self.assertTrue(all(x['magnitude_relative_l2'] == 0 for x in h))

    def test_invalid_inputs_are_rejected(self):
        a = self.s.abs()
        cases = [(a*-1, self.s, len(self.y), 2),
                 (a, self.s, len(self.y), 65), (a, self.s, len(self.y)+256, 2),
                 (a[:12], self.s, len(self.y), 2),
                 (a, self.s.real, len(self.y), 2),
                 (a*float('nan'), self.s, len(self.y), 2)]
        for args in cases:
            with self.assertRaises(ValueError): refine_phase(*args)

    def test_pcm_reference_and_float_output_keep_equal_preview_level(self):
        rng = np.random.default_rng(112)
        reference = rng.normal(size=282240)
        reference *= .1 / np.sqrt(np.mean(reference**2))
        reference = np.rint(reference*32768).astype('<i2').astype(np.float32)/32768
        generated = rng.normal(scale=.14,size=282240).astype(np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'preview'
            save_previews(folder,{'reference':reference,'generated':generated},{},pcm_scale=32768)
            levels=[]
            for name in ('reference','generated'):
                with wave.open(str(folder/(name+'.wav'))) as wav:
                    pcm=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)/32768
                levels.append(np.sqrt(np.mean(pcm**2)))
            self.assertLess(abs(levels[0]-levels[1]),5e-7)


if __name__ == '__main__':
    unittest.main()
