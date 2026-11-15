#!/usr/bin/env python3
"""生成済みの追加MF07教師を照合し、同じ小型補正モデルを再学習する。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave
import array

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge(original, generated, destination):
    from expanded_data import read_expected
    expected = read_expected(HERE / 'expanded_texts.json')
    old = json.loads((original / 'dataset.json').read_text())
    extra = json.loads((generated / 'dataset.json').read_text())
    if len(old) != 12 or len(extra) != 80:
        raise ValueError('教師の件数が異なります')
    rows = old + extra
    if len({r['sample_id'] for r in rows}) != 92:
        raise ValueError('教師IDが重複しています')
    plan = json.loads((generated / 'plan.json').read_text())
    if plan.get('selected_profiles') != ['mf07']:
        raise ValueError('追加教師の方式が異なります')
    original_manifest = json.loads((HERE / 'voice_manifest.json').read_text())
    if plan['manifest'] != original_manifest:
        raise ValueError('教師の生成条件が変更されています')
    destination.mkdir(exist_ok=False)
    for root, records in ((original, old), (generated, extra)):
        for row in records:
            e = expected.get(row['sample_id'])
            if not e or row['profile'] != 'mf07' or row['text'] != e['text'] or row['split'] != e['split']:
                raise ValueError('教師と文章計画が一致しません')
            if not 1 <= row['seconds'] <= 25:
                raise ValueError('音声の長さが不適切です')
            if root == generated and ('storage_gain' not in row or row['stored_peak'] > .980001):
                raise ValueError('追加音声にPCM保存時の余裕がありません')
            for key in ('wav', 'latent'):
                source = (root / row[key]).resolve()
                target = (destination / row[key]).resolve()
                if (not source.is_relative_to(root.resolve()) or not target.is_relative_to(destination.resolve())
                        or digest(source) != row[key + '_sha256']):
                    raise ValueError('教師の照合に失敗しました')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            with wave.open(str(root / row['wav'])) as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 48000):
                    raise ValueError('教師のPCM形式が異なります')
                pcm = array.array('h', w.readframes(w.getnframes()))
                if sys.byteorder != 'little':
                    pcm.byteswap()
            rails = sum(v >= 32767 or v <= -32768 for v in pcm)
            if root == generated and rails:
                raise ValueError('追加音声のPCMが上限に達しています')
            if rails:
                print('LEGACY_PCM_RAIL_SAMPLES', row['sample_id'], rails, flush=True)
    (destination / 'dataset.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print('MERGED', dict(Counter(r['split'] for r in rows)), sum(r['seconds'] for r in rows), flush=True)


def main(root):
    root = root.resolve()
    source = root / 'input'
    repo = root / 'work/Irodori-TTS'
    python = repo / '.venv/bin/python'
    generated = root / 'teacher'
    with (root / 'generation.log').open('x') as log:
        subprocess.run([str(python), '-u', str(source / 'pilot.py'), 'generate',
            '--work', str(root / 'work'), '--reference', str(source / 'sumi-reference-v1.sumiref'),
            '--output', str(generated), '--profile', 'mf07', '--texts', str(source / 'expanded_texts.json')],
            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=650)
    merge(source / 'data', generated, root / 'data')
    with (root / 'training.log').open('x') as log:
        subprocess.run([str(python), '-u', str(source / 'train_magnitude.py'),
            '--data', str(root / 'data'), '--base', str(source / 'base.pt'), '--output', str(root / 'output'),
            '--expanded-plan', str(source / 'expanded_texts.json'), '--device', 'cuda',
            '--steps', '5000', '--max-seconds', '240'], stdout=log, stderr=subprocess.STDOUT,
            check=True, timeout=300)
    print('EXPANDED_MAGNITUDE_COMPLETE', (root / 'output/completed.json').read_text(), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    main(parser.parse_args().root)
