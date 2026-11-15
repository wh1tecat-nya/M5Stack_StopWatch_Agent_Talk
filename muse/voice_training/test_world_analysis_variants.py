"""既知音程・無音・条件間の固定成分を検査する。"""
import unittest
import numpy as np
from world_vocoder import SR, analyze, frame_count, synthesize_full
from world_analysis_variants import CONDITIONS, analyze_variant, voicing_stats


class AnalysisVariantTests(unittest.TestCase):
    def test_known_pitch_current_parity_and_single_gate_change(self):
        t = np.arange(SR) / SR
        y = sum(.1 / n * np.sin(2 * np.pi * 220 * n * t) for n in range(1, 12))
        original = y.copy()
        current = analyze_variant(y, 'current_dio')
        for actual, expected in zip(current, analyze(y)):
            np.testing.assert_array_equal(actual, expected)
        gate = analyze_variant(y, 'dio_without_ap_gate')
        np.testing.assert_array_equal(gate[0], current[0])
        np.testing.assert_array_equal(gate[1], current[1])
        for condition in CONDITIONS:
            f0, sp, ap = analyze_variant(y, condition)
            self.assertEqual(len(f0), frame_count(len(y)))
            self.assertLess(abs(np.median(f0[f0 > 0]) - 220), 2)
            self.assertGreater(np.mean(f0 > 0), .9)
            out = synthesize_full(f0, sp, ap, len(y))
            self.assertTrue(np.isfinite(out).all())
            self.assertGreater(np.std(out), .01)
            voicing_stats(f0, ap)
        np.testing.assert_array_equal(y, original)

    def test_silence_and_invalid_inputs(self):
        y = np.zeros(SR // 2)
        for condition in CONDITIONS:
            f0, sp, ap = analyze_variant(y, condition)
            self.assertFalse(np.any(f0))
            out = synthesize_full(f0, sp, ap, len(y))
            self.assertLess(np.max(np.abs(out)), 1e-6)
        for y, c in ((np.zeros(10), 'harvest'), (np.zeros((SR, 2)), 'current_dio'),
                     (np.full(SR, np.nan), 'current_dio'), (np.zeros(SR), 'unknown')):
            with self.assertRaises(ValueError):
                analyze_variant(y, c)


if __name__ == '__main__':
    unittest.main()
