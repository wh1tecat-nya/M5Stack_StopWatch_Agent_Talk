"""評価区分や無声F0が学習統計へ混入しないことを確認する。"""
import random
import tempfile
import unittest
from pathlib import Path

import torch
from train_world import normalization, batch_segments, checkpoint, load_model
from world_model import WorldPredictor, SP_DIM, AP_DIM, CONTEXT, SEGMENT


class TrainingTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        t = 180
        self.item = {'split': 'train', 'z': torch.arange(t).float()[None].repeat(32, 1),
                     'features': torch.zeros(SP_DIM + AP_DIM, t), 'log_f0': torch.full((1, t), 5.),
                     'uv': torch.ones(1, t)}

    def test_normalization_rejects_evaluation_split(self):
        with self.assertRaises(ValueError): normalization([{**self.item, 'split': 'validation'}])

    def test_unvoiced_pitch_is_excluded_from_statistics(self):
        self.item['uv'][:, 90:] = 0
        a = normalization([self.item])
        self.item['log_f0'][:, 90:] = 999
        b = normalization([self.item])
        for x, y in zip(a, b): torch.testing.assert_close(x, y)
        self.assertEqual(float(b[-2]), 5.)

    def test_segments_keep_features_aligned(self):
        self.item['features'] = self.item['z'][:1].repeat(SP_DIM + AP_DIM, 1)
        batch = batch_segments([self.item], random.Random(112), 4)
        self.assertEqual(batch['z'].shape, (4, 32, SEGMENT + 2 * CONTEXT))
        torch.testing.assert_close(batch['z'][:, :1], batch['features'][:, :1])

    def test_checkpoint_preserves_prediction_and_buffers(self):
        model = WorldPredictor(*normalization([self.item])).eval()
        z = self.item['z'][None]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'best.pt'
            checkpoint(path, model, 3, .4)
            restored, state = load_model(path)
            self.assertEqual(state['step'], 3)
            torch.testing.assert_close(model(z), restored(z), rtol=0, atol=0)


if __name__ == '__main__': unittest.main()
