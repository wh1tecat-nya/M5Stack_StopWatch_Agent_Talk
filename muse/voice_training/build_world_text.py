#!/usr/bin/env python3
"""固定した製品版辞書のG2Pを、学習データ確認用にホストビルドする。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def build(output):
    root = Path(__file__).resolve().parents[1]
    archive = root / '.cache/sanotts-core.zip'
    expected = '22bf7fcff83db63d1cbc677e12415c40c955cdbee9c8088539855fd07b0724c6'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Pinned SanoTTS archive changed')
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'source'
    source.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if item.is_dir():
                continue
            if '/src/' not in item.filename and 'license' not in item.filename.lower():
                continue
            target = source / item.filename
            if not target.resolve().is_relative_to(source.resolve()):
                raise ValueError('Unsafe archive path')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(item))
    core = source / 'SanoTTS-jp/src/core'
    shim = output / 'host_port.h'
    shim.write_text('#pragma once\n#define SAAN_HOT_DATA\n#define SAAN_HOT_CODE\n')
    heap = output / 'host_heap.c'
    heap.write_text('''#include <stdlib.h>
#include <string.h>
void *saan_oj_calloc(size_t n,size_t s){return calloc(n,s);}
void *saan_oj_malloc(size_t n){return malloc(n);}
char *saan_oj_strdup(const char*s){return strdup(s);}
void saan_oj_free(void*p){free(p);}
''')
    flags = ['-O2', '-DSANOTTS_ENABLE_KANJI=1', '-DLABEL_IDS_EXTERNAL_SCRATCH=1',
             '-I' + str(core), '-I' + str(root / 'overlay'), '-include', str(shim)]
    units = [core / (name + '.c') for name in ['accent', 'g2p', 'jdict', 'label_ids', 'njd_rules', 'saan_kanji']]
    units += sorted((core / 'openjtalk').glob('*.c')) + [heap]
    objects = []
    for i, unit in enumerate(units):
        obj = output / f'{i}-{unit.stem}.o'
        subprocess.run(['cc', *flags, '-c', str(unit), '-o', str(obj)], check=True)
        objects.append(str(obj))
    cli = root / 'voice_training/native/world_text_cli.cpp'
    executable = output / 'world_text_cli'
    subprocess.run(['c++', '-std=c++17', *flags, str(cli), *objects, '-o', str(executable)], check=True)
    report = {'archive_sha256': expected, 'source_cli_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
              'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'purpose': 'phoneme export only, no timing or speech synthesis'}
    (output / 'build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(executable)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output.resolve())
