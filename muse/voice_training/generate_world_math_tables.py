#!/usr/bin/env python3
"""波形用の固定浮動小数点テーブルを再生成・照合する。学習重みは変更しない。"""
import argparse
import math
from pathlib import Path


def generate():
    parts = ['#pragma once\n// Generated deterministic float tables; used only by the opt-in LUT experiment.\nnamespace sumi_world_lut {\n']
    tables = [
        ('kSine', [math.sin(2 * math.pi * i / 2048) for i in range(2049)]),
        ('kLog', [math.log1p(i / 1024) for i in range(1025)]),
        ('kExp', [math.exp((i / 1024 - .5) * math.log(2)) for i in range(1025)]),
    ]
    for name, values in tables:
        parts.append(f'inline constexpr float {name}[] = {{\n')
        for i in range(0, len(values), 6):
            parts.append('  ' + ','.join(x.hex() + 'f' for x in values[i:i + 6]) + ',\n')
        parts.append('};\n')
    parts.append('}\n')
    return ''.join(parts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = Path(__file__).resolve().parent / 'native/world_math_tables.h'
    content = generate()
    if args.check:
        if target.read_text() != content:
            raise SystemExit('Table mismatch; compare the host libm before replacing frozen constants')
        print('WORLD lookup tables match')
    else:
        target.write_text(content)
