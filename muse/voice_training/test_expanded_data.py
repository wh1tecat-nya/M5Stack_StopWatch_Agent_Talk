import json
from pathlib import Path
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch
import expanded_data as e


class ExpandedTests(unittest.TestCase):
    def test_all_texts_are_unique_and_splits_fixed(self):
        expected = e.read_expected(e.HERE / 'expanded_texts.json')
        self.assertEqual(Counter(r['split'] for r in expected.values()), {'train': 72, 'validation': 10, 'test': 10})

    def test_duplicate_old_text_rejected(self):
        rows = json.loads((e.HERE / 'expanded_texts.json').read_text())
        rows[0]['text'] = json.loads((e.HERE / 'pilot_texts.json').read_text())[0]['text']
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bad.json'; p.write_text(json.dumps(rows))
            with self.assertRaises(ValueError): e.read_expected(p)

    def test_changed_evaluation_split_rejected_before_loading_audio(self):
        expected = e.read_expected(e.HERE / 'expanded_texts.json')
        rows = [{**r, 'sample_id': name, 'profile': 'mf07'} for name, r in expected.items()]
        rows[-1]['split'] = 'train'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / 'dataset.json').write_text(json.dumps(rows))
            # load_expandedのインポート先は学習パッケージの実装を使用する。
            with self.assertRaises(ValueError): e.load_expanded(root, e.HERE / 'expanded_texts.json')

    def test_normalization_uses_only_original_training_rows(self):
        original = json.loads((e.HERE / 'pilot_texts.json').read_text())
        rows = [{'row': {'sample_id': 'mf07-' + r['id']}} for r in original if r['split'] == 'train']
        extra = {'row': {'sample_id': 'mf07-expand_train_01'}}
        self.assertEqual(e.original_training_rows({'train': rows + [extra]}), rows)


if __name__ == '__main__':
    unittest.main()
