"""組み合わせ診断で成分の取り違えや入力の破壊がないことを検査する。"""
import unittest
import numpy as np
from probe_world_component_interactions import combinations, FACTORS


class InteractionTests(unittest.TestCase):
    def test_complete_factorial_preserves_sources_and_independent_buffers(self):
        tf = np.arange(62 * 10, dtype=np.float32).reshape(62, 10)
        pf = -tf - 1
        tp = np.array([0, 200, 210, 220, 0, 230, 240, 0, 250, 0.])
        pp = np.array([0, 110, 120, 0, 130, 140, 0, 150, 160, 0.])
        original = [x.copy() for x in (tf, tp, pf, pp)]
        pairs = combinations(tf, tp, pf, pp)
        self.assertEqual(len(set(FACTORS.values())), 8)
        for name, (features, f0) in pairs.items():
            e, a, p = FACTORS[name]
            np.testing.assert_array_equal(features[:60], (tf if e else pf)[:60])
            np.testing.assert_array_equal(features[60:], (tf if a else pf)[60:])
            np.testing.assert_array_equal(f0, tp if p else pp)
            for other, (of, op) in pairs.items():
                if name != other:
                    self.assertFalse(np.shares_memory(features, of))
                    self.assertFalse(np.shares_memory(f0, op))
            features[:] = 0
            f0[:] = 0
        for actual, expected in zip((tf, tp, pf, pp), original):
            np.testing.assert_array_equal(actual, expected)

    def test_rejects_unaligned_or_invalid_sources(self):
        f = np.zeros((62, 10))
        p = np.full(10, 200.)
        for args in ((f, p, f[:, :9], p), (f, p, f, p[:9]),
                     (f, p, f + np.nan, p), (f, -p, f, p)):
            with self.assertRaises(ValueError):
                combinations(*args)


if __name__ == '__main__':
    unittest.main()
