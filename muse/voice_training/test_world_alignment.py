"""Check CTC paths against exhaustive sequence enumeration, not another DP."""
import itertools
import unittest
import numpy as np
from align_world_text import ctc_path, edit_distance


def collapse(sequence):
    return [value for i, value in enumerate(sequence) if value and (i == 0 or value != sequence[i - 1])]


class AlignmentTests(unittest.TestCase):
    def test_exhaustive_oracle(self):
        rng = np.random.default_rng(782)
        for frames in range(1, 7):
            scores = rng.normal(size=(frames, 3))
            for target in ([1], [2], [1, 1], [1, 2], [1, 2, 1]):
                legal = [sequence for sequence in itertools.product(range(3), repeat=frames)
                         if collapse(sequence) == target]
                if not legal:
                    with self.assertRaises(ValueError):
                        ctc_path(scores, target)
                    continue
                expected = max(sum(scores[t, value] for t, value in enumerate(sequence)) for sequence in legal)
                path, actual = ctc_path(scores, target)
                labels = np.zeros(2 * len(target) + 1, dtype=int)
                labels[1::2] = target
                self.assertEqual(collapse(labels[path]), target)
                self.assertAlmostEqual(actual, expected, places=12)
                self.assertAlmostEqual(actual, scores[np.arange(frames), labels[path]].sum(), places=12)

    def test_invalid_input(self):
        for scores, target in [(np.zeros((2, 3)), []), (np.zeros((2, 3)), [0]),
                               (np.zeros((2, 3)), [3]), (np.full((2, 3), np.nan), [1])]:
            with self.assertRaises(ValueError):
                ctc_path(scores, target)

    def test_edit_distance(self):
        self.assertEqual(edit_distance([], [1, 2]), 2)
        self.assertEqual(edit_distance([1, 2, 3], [1, 3]), 1)
        self.assertEqual(edit_distance([1, 2], [1, 3]), 1)


if __name__ == '__main__':
    unittest.main()
