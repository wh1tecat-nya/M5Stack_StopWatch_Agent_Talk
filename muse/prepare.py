#!/usr/bin/env python3
"""Prepare pinned sources; generated/vendor files live only under .cache/."""
from pathlib import Path
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / '.cache'
REV = '463bd2a05380fea4211b0099ad0f70933c108b20'
SDK = CACHE / f'muse-gadget-sdk-{REV}'
ESP = SDK / 'esp32'
ASSETS = {
    'NotoSansCJKjp-Regular.otf': ('https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf',
                                '68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5'),
    'NotoSansCJK-OFL.txt': ('https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/LICENSE',
                          '6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2'),
    'muse-sdk.tar.gz': (f'https://codeload.github.com/facebookincubator/muse-gadget-sdk/tar.gz/{REV}',
                        '0c502709da8ea625d04fde5913e3582f75cef387ee84783f4d703e26b42acb57'),
    'sanotts-core.zip': ('https://github.com/ayutaz/sanoTTS-jp/releases/download/v1.2.0/sanoTTS-jp-arduino.zip',
                         '22bf7fcff83db63d1cbc677e12415c40c955cdbee9c8088539855fd07b0724c6'),
    'sanotts-voice.zip': ('https://github.com/ayutaz/sanoTTS-jp/releases/download/v1.2.0/sanoTTS-jp-voice-tsukuyomi-v4.zip',
                          '8fcbd6e4ee7da2b611102436567ee4acd1276d73a7f54a7a5f0e04cad8e076c4'),
    'dict.bin': ('https://github.com/ayutaz/sanoTTS-jp/releases/download/v1.2.0/k1-dict-213000-8mb-m5.bin',
                 '284eb31a4699598ffe885d1623a7215199ff610163c34ded959b0baa6cc7c925'),
}

def write_changed(path, data):
    if not path.exists() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

def copy_changed(source, destination):
    write_changed(destination, source.read_bytes())

def download(name):
    url, digest = ASSETS[name]
    path = CACHE / name
    if not path.exists():
        temporary = path.with_suffix(path.suffix + '.download')
        print(f'Downloading {name}', flush=True)
        with urllib.request.urlopen(url) as src, temporary.open('wb') as out:
            shutil.copyfileobj(src, out)
        temporary.replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError(f'Checksum mismatch: {path}; remove it and retry')
    return path

def generate_images():
    from PIL import Image
    source = (ROOT.parent / 'SumiAvatar/sumi_frames.h').read_text()
    dest = ESP / 'components/muse'
    data = ['#include <stdint.h>\n#include "sumi_assets.h"\n']
    declarations = ['#pragma once\n#include <stdint.h>\n']
    for name, variable in [('kSumiBadge', 'sumi_badge'), ('kSumiAvatar', 'sumi_portrait')]:
        body = re.search(r'\b' + name + r'\s*\[\s*\][^=]*=\s*\{(.*?)\};', source, re.S).group(1)
        raw = bytes(int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', body))
        png = Image.open(io.BytesIO(raw)).convert('RGBA')
        if name == 'kSumiAvatar':
            # Match the existing 1.18x portrait, crop (-32,-5), bottom anchoring.
            scaled = png.resize((round(png.width * 1.18), round(png.height * 1.18)), Image.Resampling.BILINEAR)
            pic = Image.new('RGBA', (360, 360), 'white'); pic.alpha_composite(scaled, (-32, -5))
        else:
            pic = Image.new('RGBA', (400, 400), 'white'); pic.alpha_composite(png, (0, 0))
        rgb = pic.convert('RGB')
        values = [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) for r, g, b in rgb.get_flattened_data()]
        declarations.append(f'extern const uint16_t {variable}[{len(values)}];\n')
        data.append(f'const uint16_t {variable}[{len(values)}] = {{\n')
        for i in range(0, len(values), 16): data.append(','.join(f'0x{x:04x}' for x in values[i:i+16]) + ',\n')
        data.append('};\n')
        pic.save(CACHE / f'{variable}.png')
    write_changed(dest / 'sumi_assets.c', ''.join(data).encode())
    write_changed(dest / 'sumi_assets.h', ''.join(declarations).encode())

def prepare():
    CACHE.mkdir(exist_ok=True)
    archive = download('muse-sdk.tar.gz')
    # Restore only files covered by our patch. Keep downloaded IDF components
    # and caches, and fail if the upstream archive ever changes.
    patch = ROOT / 'sdk.patch'
    patched = re.findall(r'^--- a/(.+)$', patch.read_text(), re.M) if patch.exists() else []
    stage = CACHE / 'patch-stage'
    with tarfile.open(archive) as src:
        if not SDK.exists(): src.extractall(CACHE, filter='data')
        for name in patched:
            member = src.getmember(f'{SDK.name}/{name}')
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.extractfile(member).read())
    if patch.exists():
        subprocess.run(['patch', '-p1', '--batch', '-i', str(patch)], cwd=stage, check=True)
        for name in patched: copy_changed(stage / name, SDK / name)
    for path in (ROOT / 'overlay').iterdir():
        if path.is_file(): copy_changed(path, ESP / 'components/muse' / path.name)
    for path in (ROOT / 'main').glob('*'):
        copy_changed(path, ESP / 'main' / path.name)
    copy_changed(ROOT / 'partitions.csv', ESP / 'partitions_sumi.csv')
    copy_changed(ROOT / 'sdkconfig.sumi', ESP / 'sdkconfig.sumi')
    lib = ESP / 'components/sanotts'
    lib.mkdir(exist_ok=True)
    for name in ['sanotts-core.zip', 'sanotts-voice.zip']:
        with zipfile.ZipFile(download(name)) as src:
            for entry in src.infolist():
                if entry.is_dir(): continue
                target = lib / entry.filename
                if not target.resolve().is_relative_to(lib.resolve()): raise RuntimeError('Unsafe archive path')
                write_changed(target, src.read(entry))
    download('dict.bin')
    copy_changed(ROOT / 'sanotts.CMakeLists.txt', lib / 'CMakeLists.txt')
    for file in (ROOT / 'sanotts-compat').rglob('*'):
        if file.is_file(): copy_changed(file, lib / 'compat' / file.relative_to(ROOT / 'sanotts-compat'))
    generate_images()
    from generate_font import generate
    font = download('NotoSansCJKjp-Regular.otf')
    download('NotoSansCJK-OFL.txt')
    generate(ESP, ROOT / 'overlay', write_changed, font)
    sys.path.insert(0, str(ROOT / 'voice_training'))
    from prepare_world_device import prepare_device
    prepare_device(ESP, os.environ.get('SUMI_WORLD_BENCH') == '1', copy_changed, write_changed)
    print(f'Prepared {ESP}')

if __name__ == '__main__': prepare()
