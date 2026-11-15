#!/usr/bin/env python3
"""Bの保存済み教師音声と固定した学習コードをColab入力へまとめる。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE_REVISION = 'f05d7ae2b4b4be362824e19e21acf6ce585ed0a6'
INITIAL_SHA = 'be5bf3f586cc6de741f91063fcf06e0b4a47ffce7f9e27cf5bd32e39fbfadcc6'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(args):
    selection = json.loads((HERE / 'baseline_selection.json').read_text())
    if selection['training_profiles'] != ['mf07']:
        raise ValueError('Bの基準選択と一致しません')
    rows = [row for row in json.loads((args.teacher / 'dataset.json').read_text()) if row['profile'] == 'mf07']
    if len(rows) != 12 or len({row['text'] for row in rows}) != 12:
        raise ValueError('Bの12文章が揃っていません')
    if sha(args.initial) != INITIAL_SHA:
        raise ValueError('学習初期値のSHA-256が一致しません')
    args.output.mkdir(parents=True, exist_ok=False)

    def copy(source, relative):
        dest = args.output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)

    for row in rows:
        for key in ('wav', 'latent'):
            source = (args.teacher / row[key]).resolve()
            if not source.is_relative_to(args.teacher.resolve()) or sha(source) != row[key + '_sha256']:
                raise ValueError('教師ファイルの照合に失敗しました')
            copy(source, Path('data') / row[key])
    (args.output / 'data/dataset.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    for name in ('train_decoder.py', 'test_decoder.py', 'baseline_selection.json',
                 'band_loss.py', 'test_band_loss.py', 'diagnose_decoder.py'):
        copy(HERE / name, name)
    modules = ['_param_reference.py', 'vocab.py', 'losses.py', 'discriminator.py']
    for name in modules:
        copy(args.source / 'src/saanotts_jp' / name, 'vendor/saanotts_jp/' + name)
    for name in ('LICENSE', 'LICENSE-MODEL.md', 'NOTICE.md'):
        copy(args.source / name, 'vendor/' + name)
    copy(args.initial, 'initial.pt')
    provenance = {
        'source_repository': 'https://github.com/ayutaz/sanoTTS-jp', 'source_revision': SOURCE_REVISION,
        'initial_url': 'https://github.com/ayutaz/sanoTTS-jp/releases/download/v1.0.0/saanotts-jp-v4-stage4.pt',
        'initial_sha256': INITIAL_SHA, 'teacher_result_archive_sha256': selection['source_result_archive_sha256'],
        'profile': 'mf07', 'baseline_sample_sha256': selection['baseline_sample']['wav_sha256'],
        'model_license': 'vendor/LICENSE-MODEL.md', 'source_license': 'vendor/LICENSE',
        'purpose': '小型decoderの再構成試験。テキストからのTTSや実機用モデルではありません。',
        'upstream_files': {name: sha(args.source / 'src/saanotts_jp' / name) for name in modules},
    }
    (args.output / 'provenance.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n')
    manifest = {str(p.relative_to(args.output)): sha(p) for p in sorted(args.output.rglob('*')) if p.is_file()}
    (args.output / 'SHA256.json').write_text(json.dumps(manifest, indent=2) + '\n')
    archive = args.output.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(args.output.rglob('*')):
            if path.is_file(): z.write(path, str(path.relative_to(args.output)))
    print(json.dumps({'zip': str(archive), 'bytes': archive.stat().st_size, 'sha256': sha(archive)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--teacher', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--initial', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args())
