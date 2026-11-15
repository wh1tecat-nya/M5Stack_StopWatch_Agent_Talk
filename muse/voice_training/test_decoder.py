"""実データの時間対応・評価分離・学習の勾配を検証する。"""
import json
from pathlib import Path
import random
import shutil
import tempfile
import unittest
import wave

import numpy as np
import torch

import train_decoder as subject

ROOT = Path(__file__).resolve().parent


class ReconstructionTests(unittest.TestCase):
    def test_latent_interpolation_preserves_constant_and_time(self):
        constant = np.ones((50, 32), dtype=np.float32)
        self.assertTrue(np.all(subject.align_latent(constant, 173) == 1))
        ramp = np.repeat(np.arange(50, dtype=np.float32)[:, None], 32, axis=1)
        z = subject.align_latent(ramp, 173)
        self.assertEqual(z[0, 0], 0)
        self.assertAlmostEqual(float(z[0, 40]), 11.255102, places=5)
        self.assertEqual(z[0, -1], 49)

    def test_actual_teacher_length_and_splits(self):
        data = subject.load_data(ROOT / 'data')
        self.assertEqual([len(data[k]) for k in ('train', 'validation', 'test')], [8, 2, 2])
        for items in data.values():
            for item in items:
                self.assertEqual(item['z'].shape[1] * subject.HOP, len(item['y']))
                self.assertLess(len(item['y']) - item['samples'], subject.HOP)

    def test_wrong_teacher_rejected_before_training(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dataset.json').write_text(json.dumps([
                {'profile': 'anime40', 'sample_id': 'wrong', 'text': '違う教師'}]))
            with self.assertRaises(ValueError): subject.load_data(root)

    def test_pcm_storage_gain_restores_training_amplitude(self):
        original = subject.load_data(ROOT / 'data')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            shutil.copytree(ROOT / 'data', root)
            rows = json.loads((root / 'dataset.json').read_text())
            row = rows[0]
            path = root / row['wav']
            with wave.open(str(path)) as wav:
                params = wav.getparams()
                pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
            with wave.open(str(path), 'wb') as wav:
                wav.setparams(params)
                wav.writeframes(np.rint(pcm.astype(np.float32) * .5).astype('<i2').tobytes())
            row.update(storage_gain=.5, wav_sha256=subject.digest(path))
            (root / 'dataset.json').write_text(json.dumps(rows))
            restored = subject.load_data(root)
            for split in original:
                for before, after in zip(original[split], restored[split]):
                    self.assertTrue(torch.equal(before['z'], after['z']))
                    self.assertLess(float((before['y'] - after['y']).abs().max()), 6e-5)
            row['storage_gain'] = 0
            (root / 'dataset.json').write_text(json.dumps(rows))
            with self.assertRaisesRegex(ValueError, 'PCM保存ゲイン'):
                subject.load_data(root)

    def test_statistics_depend_only_on_training(self):
        train = [{'z': torch.full((32, 3), 2.)}, {'z': torch.full((32, 3), 4.)}]
        mean, std = subject.normalization(train)
        self.assertTrue(torch.equal(mean, torch.full((1, 32, 1), 3.)))
        self.assertTrue(torch.equal(std, torch.ones(1, 32, 1)))

    def test_crop_keeps_waveform_and_latent_time(self):
        z = torch.arange(300).repeat(32, 1).float()
        y = torch.arange(300 * subject.HOP).float()
        batch_z, batch_y = subject.batch_segments([{'z': z, 'y': y}], random.Random(2), 2, 'cpu')
        self.assertEqual(batch_y.shape, (2, subject.SEGMENT * subject.HOP))
        for i in range(2):
            start = int(batch_z[i, 0, 0]) + subject.CONTEXT
            self.assertEqual(float(batch_y[i, 0]), start * subject.HOP)

    def test_real_decoder_backward_is_finite(self):
        torch.set_num_threads(2)
        torch.manual_seed(12)
        data = subject.load_data(ROOT / 'data')
        model = subject.Reconstruction(*subject.normalization(data['train']))
        initial = torch.load(ROOT / 'initial.pt', map_location='cpu', weights_only=True)
        model.decoder.load_state_dict(initial['state']['decoder'], strict=True)
        z, y = subject.batch_segments(data['train'], random.Random(4), 1, 'cpu')
        result = model(z)[:, subject.CONTEXT * subject.HOP:(subject.CONTEXT + subject.SEGMENT) * subject.HOP]
        self.assertEqual(result.shape, y.shape)
        loss, _ = subject.generator_loss(result, y)
        self.assertTrue(torch.isfinite(loss))
        loss.backward()
        for module in (model.adapter, model.decoder):
            grads = [p.grad for p in module.parameters() if p.grad is not None]
            self.assertTrue(grads)
            self.assertTrue(all(torch.isfinite(g).all() for g in grads))
            self.assertGreater(sum(float(g.abs().sum()) for g in grads), 0)

    def test_adversarial_phase_backpropagates_to_generator(self):
        torch.set_num_threads(2)
        torch.manual_seed(4)
        disc = subject.FirstDifferenceDiscriminator()
        target = torch.randn(1, subject.SEGMENT * subject.HOP) * .1
        fake = (torch.randn_like(target) * .1).requires_grad_()
        dloss, _ = subject.discriminator_loss(disc(target)[0], disc(fake.detach())[0])
        dloss.backward()
        self.assertTrue(torch.isfinite(dloss))
        self.assertTrue(all(torch.isfinite(p.grad).all() for p in disc.parameters() if p.grad is not None))
        disc.eval().requires_grad_(False)
        with torch.no_grad(): _, features = disc(target)
        prediction, fake_features = disc(fake)
        loss, _ = subject.generator_loss(fake, target, prediction, fake_features, features)
        loss.backward()
        self.assertTrue(torch.isfinite(fake.grad).all())
        self.assertGreater(float(fake.grad.abs().sum()), 0)


if __name__ == '__main__':
    unittest.main()
