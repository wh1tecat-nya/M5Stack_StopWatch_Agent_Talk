import unittest

import numpy as np
import torch

from world_text_adapter import TextAdapter, experimental_boundaries, frame_features, frozen_projection, phone_selection, predicted_phone_boundaries


class TextFeatureTest(unittest.TestCase):
    def test_token_changes_and_exact_end_remain_finite(self):
        tokens = np.stack([np.array([1, 2, 3], dtype=np.float32)] * 48)
        x = frame_features(tokens, [0, .1, .3, .4], [0, .05, .1, .3, .4])
        np.testing.assert_array_equal(x[0], [1, 1, 2, 3, 3])
        self.assertTrue(np.isfinite(x).all())
        self.assertEqual(x.shape, (54, 5))

    def test_zero_duration_and_negative_time_rejected(self):
        for boundaries, times in [([0, 0, .2], [0]), ([0, .1, .2], [-.1]),
                                  ([0, .1, .2], [.1, 0])]:
            with self.assertRaises(ValueError):
                frame_features(np.zeros((48, 2)), boundaries, times)

    def test_control_tokens_do_not_create_overlapping_intervals(self):
        row = {'token_ids': list(range(7)), 'seconds': 1,
               'phonemes': [{'token_index': 1, 'start_seconds': .1, 'end_seconds': .3},
                            {'token_index': 5, 'start_seconds': .6, 'end_seconds': .9}]}
        bounds = experimental_boundaries(row)
        self.assertEqual(len(bounds), 8)
        self.assertTrue(np.all(np.diff(bounds) > 0))
        self.assertEqual((bounds[0], bounds[-1]), (0, 1))
        row['phonemes'][1]['token_index'] = 7
        with self.assertRaises(ValueError):
            experimental_boundaries(row)

    def test_both_encoder_widths_and_short_sequences(self):
        for channels in (46, 54):
            model = TextAdapter(torch.zeros(1, channels, 1), torch.ones(1, channels, 1),
                                torch.zeros(1, 32, 1), torch.ones(1, 32, 1))
            output = model.physical(torch.zeros(1, channels, 1))
            self.assertEqual(output.shape, (1, 32, 1))
            self.assertTrue(torch.isfinite(output).all())

    def test_phone_only_removes_inserted_blanks_but_keeps_pause(self):
        names = ['^', '_', 'a', '_', '[', '_', 'k', '_', '_', 'o', '_', '$']
        row = {'token_ids': [{'token': name} for name in names], 'seconds': 1,
               'phonemes': [{'token_index': i, 'start_seconds': a, 'end_seconds': b}
                            for i, a, b in [(2, .1, .2), (6, .4, .5), (9, .8, .9)]]}
        selected, bounds = phone_selection(row)
        np.testing.assert_array_equal(selected, [0, 2, 6, 8, 9, 11])
        self.assertTrue(np.all(np.diff(bounds) > 0))
        self.assertAlmostEqual(bounds[2], (.15 + .45) / 2)

    def test_direct_acoustic_output_width(self):
        model = TextAdapter(torch.zeros(1, 54, 1), torch.ones(1, 54, 1),
                            torch.zeros(1, 64, 1), torch.ones(1, 64, 1))
        self.assertEqual(model.physical(torch.zeros(1, 54, 3)).shape, (1, 64, 3))

    def test_duration_prediction_needs_no_teacher_alignment(self):
        ids = [{'token': s} for s in ['^', '_', 'a', '_', '[', '_', 'k', '_', '$']]
        selected, bounds = predicted_phone_boundaries(ids, np.zeros(len(ids)))
        np.testing.assert_array_equal(selected, [0, 2, 6, 8])
        self.assertTrue(np.all(np.diff(bounds) > 0))
        self.assertAlmostEqual(bounds[-1], len(ids) * 256 / 22050)
        for bad in [np.zeros(2), np.full(len(ids), np.nan), np.full(len(ids), 30)]:
            with self.assertRaises(ValueError):
                predicted_phone_boundaries(ids, bad)

    def test_frozen_projection_updates_input_not_voice_weights(self):
        class Voice(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.base = torch.nn.Conv1d(32, 64, 1)

            def from_base(self, z, prediction):
                return prediction + z.mean(1, keepdim=True)

        voice = Voice().requires_grad_(False)
        before = {k: v.clone() for k, v in voice.state_dict().items()}
        z = torch.randn(1, 32, 10, requires_grad=True)
        frozen_projection(voice, z).square().mean().backward()
        self.assertIsNotNone(z.grad)
        self.assertGreater(float(z.grad.abs().sum()), 0)
        for name, value in voice.state_dict().items():
            torch.testing.assert_close(value, before[name], rtol=0, atol=0)
        self.assertTrue(all(p.grad is None for p in voice.parameters()))


if __name__ == '__main__':
    unittest.main()
