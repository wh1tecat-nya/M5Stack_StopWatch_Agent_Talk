#!/usr/bin/env python3
"""固定した本体用テキストエンコーダーの中間特徴を取り出す。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from build_world_text import build as build_text


def build(output):
    build_text(output)
    core = output / 'source/SanoTTS-jp/src/core'
    flags = ['-O2', '-ffp-contract=off', '-DSAAN_PORT_HEADER="host_port.h"', '-I' + str(output), '-I' + str(core), '-include', str(output / 'host_port.h')]
    objects = []
    sources = ['saanotts', 'saanotts_int8', 'fft']
    wrapper = output / 'world_text_core.c'
    wrapper.write_text('''#include "saanotts.c"
saan_status sumi_text_acoustic(const saan_weights *w, saan_arena *a, const int32_t *ids,
    int count, const int32_t *durations, int frames, float *output) {
    return run_acoustic(w, a, ids, count, durations, frames, output);
}
''')
    for name in sources:
        obj = output / (name + '.o')
        unit = wrapper if name == 'saanotts' else core / (name + '.c')
        subprocess.run(['cc', *flags, '-c', str(unit), '-o', str(obj)], check=True)
        objects.append(str(obj))
    cli = Path(__file__).parent / 'native/world_text_features_cli.cpp'
    executable = output / 'world_text_features_cli'
    subprocess.run(['c++', '-std=c++17', *flags, str(cli), *objects, '-o', str(executable)], check=True)
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    report = {'source_sha256': sha(cli), 'executable_sha256': sha(executable),
              'sources': {name: sha(core / (name + '.c')) for name in sources},
              'g2p_build': json.loads((output / 'build.json').read_text()),
              'purpose': 'Existing text encoder features and duration predictions; no new voice synthesis'}
    (output / 'features_build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(executable)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output.resolve())
