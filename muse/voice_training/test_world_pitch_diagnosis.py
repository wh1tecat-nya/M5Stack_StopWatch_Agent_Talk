"""声の高さと音色の取り違え、無声区間の数値処理を検査する。"""
import unittest
import numpy as np
from world_pitch_diagnosis import component_pairs, pitch_metrics, runs, SP_DIM, AP_DIM


class PitchDiagnosisTests(unittest.TestCase):
    def test_swaps_preserve_voicing_and_timbre_sources_without_mutation(self):
        teacher = np.full((SP_DIM + AP_DIM, 6), 2.)
        predicted = np.full_like(teacher, 5.)
        rf0, pf0 = np.array([220, 0, 220, 220, 0, 0.]), np.array([330, 330, 0, 330, 0, 0.])
        pairs = component_pairs(teacher, rf0, predicted, pf0)
        np.testing.assert_array_equal(pairs['teacher_pitch_model_timbre'][0], predicted)
        np.testing.assert_array_equal(pairs['teacher_pitch_model_timbre'][1], rf0)
        np.testing.assert_array_equal(pairs['model_pitch_teacher_timbre'][0], teacher)
        np.testing.assert_array_equal(pairs['model_pitch_teacher_timbre'][1], pf0)
        pairs['teacher_pitch_model_timbre'][1][0] = 999
        self.assertEqual(rf0[0], 220)
        self.assertEqual(pairs['teacher_all'][1][0], 220)

    def test_constant_octave_error_has_no_spurious_wobble(self):
        report = pitch_metrics(np.full(10, 200.), np.full(10, 400.))
        self.assertAlmostEqual(report['pitch_mae_cents_joint_voiced'], 1200)
        self.assertAlmostEqual(report['step_error_mae_cents'], 0)
        self.assertEqual(report['predicted_uv_transitions'], 0)

    def test_voicing_boundaries_are_not_counted_as_pitch_jumps(self):
        r = np.array([200., 200, 0, 0, 200, 200])
        p = np.array([220., 220, 220, 0, 220, 220])
        m = pitch_metrics(r, p)
        self.assertAlmostEqual(m['uv_error_fraction'], 1 / 6)
        self.assertAlmostEqual(m['step_error_mae_cents'], 0)
        self.assertEqual(m['joint_voiced_frames'], 4)
        self.assertEqual(runs(r > 0), [(True, 2), (False, 2), (True, 2)])

    def test_all_unvoiced_has_no_pitch_statistic(self):
        m = pitch_metrics(np.zeros(10), np.zeros(10))
        self.assertIsNone(m['pitch_mae_cents_joint_voiced'])
        self.assertIsNone(m['step_error_mae_cents'])
        self.assertEqual(m['uv_error_fraction'], 0)

    def test_invalid_inputs_are_rejected(self):
        f = np.zeros((SP_DIM + AP_DIM, 5)); p = np.full(5, 200.)
        for args in [(f, p, f[:, :4], p), (f, p, f, p[:4]), (f, p, f, p * np.nan), (f, -p, f, p)]:
            with self.assertRaises(ValueError): component_pairs(*args)
        for args in [(p, p[:4]), (p, -p), (p, p * np.nan)]:
            with self.assertRaises(ValueError): pitch_metrics(*args)


if __name__ == '__main__': unittest.main()
