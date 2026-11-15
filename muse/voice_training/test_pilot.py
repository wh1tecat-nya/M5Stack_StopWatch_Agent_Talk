import copy
import functools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pilot


class PlanTests(unittest.TestCase):
    def test_storage_gain_preserves_quiet_audio_and_leaves_headroom(self):
        self.assertEqual(pilot.pcm_storage_gain(.5), 1.)
        for peak in (1., 1.0048, 2.):
            gain = pilot.pcm_storage_gain(peak)
            self.assertAlmostEqual(gain * peak, .98)
            self.assertAlmostEqual((peak * gain) / gain, peak)
        for peak in (float('nan'), float('inf'), -1., 5.):
            with self.assertRaises(ValueError): pilot.pcm_storage_gain(peak)

    def test_observer_targets_decorated_implementation(self):
        def decorator(function):
            @functools.wraps(function)
            def wrapper(*args, **kwargs):
                return function(*args, **kwargs)
            return wrapper

        class Codec:
            @decorator
            def decode_latent(self, latent):
                return latent

        code = pilot.implementation_code(Codec().decode_latent)
        self.assertEqual(code.co_varnames[:2], ('self', 'latent'))
        self.assertNotEqual(code, Codec.decode_latent.__code__)

    def setUp(self):
        self.manifest, self.texts = pilot.read_plan()

    def temporary_plan(self, manifest, texts):
        root = tempfile.TemporaryDirectory()
        path = Path(root.name)
        (path / 'voice_manifest.json').write_text(json.dumps(manifest))
        (path / 'pilot_texts.json').write_text(json.dumps(texts))
        self.addCleanup(root.cleanup)
        return path

    def test_missing_teacher_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['profiles'].pop()
        with patch.object(pilot, 'HERE', self.temporary_plan(manifest, self.texts)):
            with self.assertRaises(ValueError):
                pilot.read_plan()

    def test_same_text_cannot_leak_to_test(self):
        texts = self.texts + [{'id': 'leak', 'split': 'test', 'text': self.texts[0]['text']}]
        with patch.object(pilot, 'HERE', self.temporary_plan(self.manifest, texts)):
            with self.assertRaises(ValueError):
                pilot.read_plan()

    def test_reference_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reference.sumiref'
            path.write_bytes(b'not the approved voice')
            with self.assertRaises(ValueError):
                pilot.read_reference(path, self.manifest)

    def test_results_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            pilot.save_json(path, {'completed': True})
            with self.assertRaises(FileExistsError):
                pilot.save_json(path, {'completed': False})
            self.assertTrue(json.loads(path.read_text())['completed'])

    def test_profile_selection_keeps_all_original_conditions(self):
        selected = pilot.selected_profiles(self.manifest, 'mf07')
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0], self.manifest['profiles'][1])
        self.assertEqual(len(pilot.selected_profiles(self.manifest, None)), 3)
        with self.assertRaises(ValueError):
            pilot.selected_profiles(self.manifest, 'unknown')

    def test_alternate_text_plan_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new.json'
            path.write_text(json.dumps([{'id': 'extra_1', 'split': 'train', 'text': '新しい文章。'}]))
            _, texts = pilot.read_plan(path)
            self.assertEqual(texts[0]['id'], 'extra_1')
        self.assertEqual(len(pilot.read_plan()[1]), 12)


if __name__ == '__main__':
    unittest.main()
