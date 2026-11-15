"""文脈拡張で既存モデルや分割推論が変わらないことを確認する。"""
from pathlib import Path
import tempfile
import unittest

import torch
from world_model import WorldPredictor
from world_context_model import configure, load, save, HALO


class ContextTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(413)
        self.model = WorldPredictor(torch.zeros(1,32,1),torch.ones(1,32,1),
                                    torch.zeros(1,62,1),torch.ones(1,62,1),torch.tensor(5.),torch.tensor(.3)).eval()

    def test_control_is_identical_and_expansion_preserves_size(self):
        z = torch.randn(1,32,300)
        with torch.no_grad():
            original = self.model(z)
            torch.testing.assert_close(configure(self.model,'control')(z), original, atol=0, rtol=0)
        parameters = sum(p.numel() for p in self.model.parameters())
        configure(self.model,'context')
        self.assertEqual(parameters, sum(p.numel() for p in self.model.parameters()))
        self.assertEqual(parameters, 78720)
        self.assertEqual(3 + 6 * sum(b.depth.dilation[0] for b in self.model.blocks), 93)

    def test_context_crop_matches_full_and_checkpoint_keeps_mode(self):
        model = configure(self.model,'context')
        z = torch.randn(1,32,400)
        with torch.no_grad():
            full = model(z)
            chunk = model(z[:,:,90-HALO:200+HALO])[:,:,HALO:-HALO]
        torch.testing.assert_close(chunk, full[:,:,90:200], atol=2e-6, rtol=2e-6)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'model.pt'
            save(path, model, 'context', 1, .5)
            restored, state = load(path)
            self.assertEqual(state['condition'], 'context')
            with torch.no_grad():
                torch.testing.assert_close(restored(z), full, atol=0, rtol=0)
            with self.assertRaises(ValueError):
                load(path, initial_condition='control')


if __name__ == '__main__':
    unittest.main()
