"""追加文章の分割を固定し、元の学習8文の正規化を維持する。"""
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_expected(plan_path):
    original = json.loads((HERE / 'pilot_texts.json').read_text())
    extra = json.loads(Path(plan_path).read_text())
    if Counter(r['split'] for r in extra) != {'train': 64, 'validation': 8, 'test': 8}:
        raise ValueError('追加文章の分割が異なります')
    rows = original + extra
    expected = {'mf07-' + r['id']: r for r in rows}
    if len(expected) != 92 or len({r['text'] for r in rows}) != 92:
        raise ValueError('文章が重複しています')
    return expected


def load_expanded(root, plan_path):
    from train_decoder import load_data
    expected = read_expected(plan_path)
    rows = json.loads((root / 'dataset.json').read_text())
    if len(rows) != len(expected) or {r['sample_id'] for r in rows} != set(expected):
        raise ValueError('生成結果と文章計画が一致しません')
    for row in rows:
        e = expected[row['sample_id']]
        if row['text'] != e['text'] or row['split'] != e['split'] or row['profile'] != 'mf07':
            raise ValueError('文章・評価区分・教師が変更されています')
    return load_data(root, {'train': 72, 'validation': 10, 'test': 10})


def original_training_rows(data):
    expected = {'mf07-' + r['id'] for r in json.loads((HERE / 'pilot_texts.json').read_text()) if r['split'] == 'train'}
    rows = [r for r in data['train'] if r['row']['sample_id'] in expected]
    if len(rows) != 8:
        raise ValueError('固定モデルの元学習データが不足しています')
    return rows
