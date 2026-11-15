"""移植用の重み保存で出力が変わらず、破損や範囲違いを拒否することを確認する。"""
from pathlib import Path
import json
import tempfile
import unittest
import torch
from world_envelope_export import template,export,restore


class ExportTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2);torch.manual_seed(621)
        self.model=template()
        torch.nn.init.normal_(self.model.output.weight,std=.01)

    def test_roundtrip_exactly_preserves_all_state_and_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'export';meta=export(self.model,root,'0'*64);restored,_=restore(root)
            self.assertEqual(meta['bytes'],459304)
            for k,v in self.model.state_dict().items():torch.testing.assert_close(v,restored.state_dict()[k],atol=0,rtol=0)
            z=torch.randn(1,32,240)
            with torch.no_grad():torch.testing.assert_close(self.model(z),restored(z),atol=0,rtol=0)

    def test_corruption_and_overlapping_ranges_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'export';export(self.model,root,'0'*64)
            raw=(root/'weights.f32').read_bytes()
            (root/'weights.f32').write_bytes(bytes([raw[0]^1])+raw[1:])
            with self.assertRaises(ValueError):restore(root)
            (root/'weights.f32').write_bytes(raw)
            meta=json.loads((root/'model.json').read_text());meta['tensors'][1]['offset_bytes']=0
            (root/'model.json').write_text(json.dumps(meta))
            with self.assertRaises(ValueError):restore(root)


if __name__=='__main__':unittest.main()
