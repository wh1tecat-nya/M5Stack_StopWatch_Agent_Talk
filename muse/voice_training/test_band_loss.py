"""帯域を無視する損失、非有限勾配、音程診断の回帰を防ぐ。"""
import unittest
import numpy as np
import torch
from band_loss import balanced_spectral_loss
from diagnose_decoder import pitch_track, measurements, SR


class BandLossTests(unittest.TestCase):
    def test_silence_and_identity_are_finite(self):
        torch.set_num_threads(2)
        for y in (torch.zeros(1, 8192), torch.randn(1, 8192) * .01):
            x = y.clone().requires_grad_()
            loss = balanced_spectral_loss(x, y)
            loss.backward()
            self.assertLess(float(loss.detach()), .001)
            self.assertTrue(torch.isfinite(x.grad).all())

    def test_missing_quiet_high_band_is_penalized(self):
        t = torch.arange(16384) / SR
        low = .1 * torch.sin(2 * torch.pi * 220 * t)
        high = .01 * torch.sin(2 * torch.pi * 5000 * t)
        target = (low + high)[None]
        complete = balanced_spectral_loss(target, target)
        missing = balanced_spectral_loss(low[None], target)
        self.assertGreater(float(missing), float(complete) + .05)

    def test_frequency_error_and_gradient(self):
        t = torch.arange(16384) / SR
        target = (.1 * torch.sin(2 * torch.pi * 280 * t))[None]
        wrong = (.1 * torch.sin(2 * torch.pi * 258 * t))[None].requires_grad_()
        loss = balanced_spectral_loss(wrong, target)
        loss.backward()
        self.assertGreater(float(loss.detach()), .05)
        self.assertTrue(torch.isfinite(wrong.grad).all())
        self.assertGreater(float(wrong.grad.abs().sum()), 0.)

    def test_pitch_tracks_low_and_high_tones(self):
        t = np.arange(SR) / SR
        for frequency in (130, 250, 400, 550):
            pitch, _ = pitch_track(.1 * np.sin(2*np.pi*frequency*t))
            self.assertAlmostEqual(float(np.nanmedian(pitch)), frequency, delta=1.)

    def test_modulation_metric_detects_hop_period(self):
        t = np.arange(SR * 2) / SR
        carrier = .1 * np.sin(2*np.pi*5000*t)
        clean, _ = measurements(carrier)
        pulsed, _ = measurements(carrier * (1 + .5*np.sin(2*np.pi*SR/256*t)))
        self.assertGreater(pulsed['envelope_86hz_prominence_db'], clean['envelope_86hz_prominence_db'] + 10.)


if __name__ == '__main__': unittest.main()
