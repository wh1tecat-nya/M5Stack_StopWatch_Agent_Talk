"""音量差・周波数の混入・無音区間を区別できることを人工信号で確認する。"""
import unittest
import torch
from diagnose_magnitude import metrics, bounded_oracle


class DiagnosisTests(unittest.TestCase):
    def test_frame_gain_removes_only_level_changes(self):
        target = torch.tensor([[1., 2., 1.], [.1, .2, .1], [.3, .6, .3]])
        report = metrics(target * torch.tensor([.5, 2., 1.5]), target)
        self.assertGreater(report['relative_l2'], .5)
        self.assertLess(report['relative_l2_after_best_per_frame_gain'], 1e-6)

    def test_frequency_contamination_survives_gain_fit(self):
        target = torch.tensor([[1., 1.], [.001, .001], [.001, .001]])
        predicted = target + torch.tensor([[0.], [.2], [.2]])
        report = metrics(predicted, target)
        self.assertGreater(report['relative_l2_after_best_per_frame_gain'], .2)
        self.assertGreater(report['predicted_active_energy_in_target_weak_bins_fraction'], .05)
        self.assertLess(report['target_active_energy_in_same_bins_fraction'], .00001)

    def test_quiet_frame_noise_stays_separate(self):
        target = torch.tensor([[1., 0., 1.], [.1, 0., .1]])
        predicted = target.clone()
        predicted[:, 1] = .1
        report = metrics(predicted, target)
        self.assertEqual(report['quiet_frames'], 1)
        self.assertEqual(report['target_quiet_energy_fraction'], 0)
        self.assertGreater(report['predicted_quiet_energy_fraction'], 0)
        self.assertEqual(report['predicted_active_energy_in_target_weak_bins_fraction'], 0)

    def test_bound_preserves_reachable_target(self):
        base = torch.full((3, 4), .1)
        target = torch.tensor([[.001], [.05], [.5]]).expand(3, 4)
        torch.testing.assert_close(bounded_oracle(base, target), target)

    def test_bound_exposes_unreachable_target(self):
        base, target = torch.zeros(2, 3), torch.full((2, 3), 10.)
        bounded = bounded_oracle(base, target)
        self.assertTrue((bounded < target).all())
        self.assertGreater(metrics(bounded, target)['relative_l2'], .8)

    def test_invalid_and_silent_inputs_rejected(self):
        valid = torch.ones(3, 4)
        for bad in (torch.zeros(3, 4), torch.full((3, 4), float('nan')),
                    -valid, torch.ones(3, 3), torch.ones(3)):
            with self.assertRaises(ValueError):
                metrics(valid, bad)


if __name__ == '__main__':
    unittest.main()
