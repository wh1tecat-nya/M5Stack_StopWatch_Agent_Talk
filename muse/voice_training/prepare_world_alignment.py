#!/usr/bin/env python3
"""固定版の日本語音素解析モデルを取得・照合する。実機には搭載しない。"""
import hashlib
import json
from pathlib import Path
import urllib.request


def prepare():
    here = Path(__file__).resolve().parent
    manifest_path = here / 'alignment_source_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    root = here.parent / '.cache/japanese-phoneme-aligner'
    root.mkdir(parents=True, exist_ok=True)
    for record in manifest['files']:
        target = root / record['file']
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe model path')
        candidate = target
        if not target.exists():
            candidate = target.with_suffix(target.suffix + '.part')
            with urllib.request.urlopen(record['url'], timeout=60) as response, candidate.open('wb') as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
        if candidate.stat().st_size != record['bytes'] or hashlib.sha256(candidate.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Alignment asset changed: ' + record['file'])
        if candidate != target:
            candidate.replace(target)
    (root / 'source.json').write_bytes(manifest_path.read_bytes())
    return root


if __name__ == '__main__':
    print(prepare())
