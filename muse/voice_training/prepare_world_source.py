#!/usr/bin/env python3
"""PyWorld 0.3.5と同じWORLDの必要ソースを取得し、全ファイルを照合する。"""
import hashlib
import json
from pathlib import Path
import urllib.request


def prepare():
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / 'world_source_manifest.json').read_text())
    root = here.parent / '.cache' / ('world-' + manifest['revision'])
    for row in manifest['files']:
        target = root / row['path']
        if target.exists():
            data = target.read_bytes()
        else:
            url = ('https://raw.githubusercontent.com/mmorise/World/'
                   + manifest['revision'] + '/' + row['path'])
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('WORLD source mismatch: ' + row['path'])
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return root


if __name__ == '__main__':
    print(prepare())
