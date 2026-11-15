"""包絡補正の独立性、時間軸と形状損失を検証する。"""
from pathlib import Path
import tempfile
import unittest
import torch
from world_model import WorldPredictor
from world_context_model import configure
from world_envelope_model import EnvelopeRefiner,load,save,INFERENCE_HALO,TRAIN_HALO
from world_envelope_loss import EnvelopeLoss


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2);torch.manual_seed(817)
        base=configure(WorldPredictor(torch.zeros(1,32,1),torch.ones(1,32,1),torch.zeros(1,62,1),torch.ones(1,62,1),torch.tensor(5.),torch.tensor(.3)),'context')
        self.model=EnvelopeRefiner(base).eval();self.z=torch.randn(2,32,400)

    def test_only_envelope_changes_after_training(self):
        m=self.model
        with torch.no_grad():before=m.base(self.z)
        torch.testing.assert_close(m(self.z),before,atol=0,rtol=0)
        frozen={k:v.clone() for k,v in m.base.state_dict().items()}
        optimizer=torch.optim.AdamW((p for p in m.parameters() if p.requires_grad),lr=.001)
        for _ in range(2):
            loss=(m(self.z)[:,:60]-torch.randn(2,60,400)).square().mean()
            optimizer.zero_grad();loss.backward();optimizer.step()
        after=m(self.z)
        self.assertFalse(torch.equal(before[:,:60],after[:,:60]))
        torch.testing.assert_close(before[:,60:],after[:,60:],atol=0,rtol=0)
        for k,v in m.base.state_dict().items():torch.testing.assert_close(v,frozen[k],atol=0,rtol=0)

    def test_cached_base_chunk_and_saved_model_match(self):
        m=self.model
        torch.nn.init.normal_(m.output.weight,std=.02)
        with torch.no_grad():
            full=m(self.z);cached=m.base(self.z)
            torch.testing.assert_close(m.from_base(self.z,cached),full,atol=0,rtol=0)
            chunk=m(self.z[:,:,90-INFERENCE_HALO:240+INFERENCE_HALO])[:,:,INFERENCE_HALO:-INFERENCE_HALO]
            torch.testing.assert_close(chunk,full[:,:,90:240],atol=2e-6,rtol=2e-6)
            chunk=m.from_base(self.z[:,:,90-TRAIN_HALO:240+TRAIN_HALO],cached[:,:,90-TRAIN_HALO:240+TRAIN_HALO])[:,:,TRAIN_HALO:-TRAIN_HALO]
            torch.testing.assert_close(chunk,full[:,:,90:240],atol=2e-6,rtol=2e-6)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.pt';save(path,m,'focused',1,.5);restored,_=load(path)
            with torch.no_grad():torch.testing.assert_close(restored(self.z),full,atol=0,rtol=0)

    def test_shape_ignores_uniform_level_but_envelope_detects_it(self):
        loss=EnvelopeLoss();m=self.model
        target=torch.randn(1,62,40)*.01
        p=torch.cat((target,torch.zeros(1,2,40)),1).clone().requires_grad_(True)
        w=torch.ones(1,1,40)
        value,terms=loss(p,target,m,w)
        self.assertEqual(float(value.detach()),0.)
        # 第0係数は復号後の全周波数へ同じ増分を与える。
        shifted=p.detach().clone();shifted[:,0]+=.1;shifted.requires_grad_(True)
        value,terms=loss(shifted,target,m,w)
        self.assertGreater(float(terms['envelope'].detach()),0)
        self.assertLess(float(terms['shape'].detach()),1e-10)
        value.backward();self.assertTrue(torch.isfinite(shifted.grad).all())

    def test_quiet_frames_are_downweighted_without_dropping_voiced_frames(self):
        loss=EnvelopeLoss();features=torch.zeros(1,62,40);uv=torch.zeros(1,1,40)
        features[:,0,:20]=-30;uv[:,:,0]=1
        w=loss.frame_weights(features,uv)
        self.assertEqual(float(w[0,0,0]),1.)
        self.assertLess(float(w[0,0,10]),float(w[0,0,30]))
        self.assertTrue(torch.all((w>=.1)&(w<=1.)))


if __name__=='__main__':unittest.main()
