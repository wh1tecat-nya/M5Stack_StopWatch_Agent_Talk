"""容量拡張の初期出力、学習可能性、分割推論と保存形式を検証する。"""
from pathlib import Path
import tempfile
import unittest

import torch
from world_model import WorldPredictor
from world_capacity_model import initialize, load, save, HALO


class CapacityTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(726)
        self.base = WorldPredictor(torch.zeros(1,32,1),torch.ones(1,32,1),
                                  torch.zeros(1,62,1),torch.ones(1,62,1),torch.tensor(5.),torch.tensor(.3)).eval()
        self.state = {'model':self.base.state_dict()}
        self.z = torch.randn(2,32,350)

    def test_preserves_initial_predictions_and_increases_capacity(self):
        with torch.no_grad():
            expected = self.base(self.z)
            control = initialize(self.state,'control')
            wide = initialize(self.state,'capacity')
            torch.testing.assert_close(control(self.z),expected,atol=0,rtol=0)
            torch.testing.assert_close(wide(self.z),expected,atol=2e-6,rtol=2e-6)
        self.assertEqual(sum(p.numel() for p in control.parameters()),78720)
        self.assertEqual(sum(p.numel() for p in wide.parameters()),167200)

    def test_duplicated_features_can_learn_different_values(self):
        model = initialize(self.state,'capacity')
        before = model.input.weight.detach().clone()
        duplicate = next(i for i in range(64) if torch.equal(before[i],before[64]))
        target = torch.randn(2,64,350)
        optimizer = torch.optim.AdamW(model.parameters(),lr=.0002)
        loss = (model(self.z)-target).square().mean()
        loss.backward()
        self.assertTrue(all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters()))
        self.assertFalse(torch.equal(model.input.weight.grad[duplicate],model.input.weight.grad[64]))
        optimizer.step()
        self.assertFalse(torch.equal(model.input.weight[duplicate],model.input.weight[64]))

    def test_chunk_and_checkpoint_preserve_predictions(self):
        model = initialize(self.state,'capacity')
        with torch.no_grad():
            full = model(self.z)
            chunk = model(self.z[:,:,70-HALO:250+HALO])[:,:,HALO:-HALO]
        torch.testing.assert_close(chunk,full[:,:,70:250],atol=2e-6,rtol=2e-6)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'model.pt'
            save(p,model,'capacity',10,.5)
            restored,state = load(p)
            self.assertEqual(state['width'],96)
            with torch.no_grad():
                torch.testing.assert_close(restored(self.z),full,atol=0,rtol=0)
            with self.assertRaises(ValueError):
                load(p,initial_condition='control')


if __name__ == '__main__':
    unittest.main()
