#!/usr/bin/env python3
"""SiLU候補の区分3次補間係数を生成する。採用前に予測・波形差を確認する。"""
import argparse
import math
from pathlib import Path


def generate():
    def value(x):
        sigmoid = 1 / (1 + math.exp(-x))
        return x * sigmoid, sigmoid + x * sigmoid * (1 - sigmoid)
    lines = ['#pragma once\nnamespace sumi_activation {\n',
             'inline constexpr float kCubic[512][4] = {\n']
    for i in range(512):
        x = -16 + i / 16
        a, da = value(x)
        b, db = value(x + 1 / 16)
        da /= 16
        db /= 16
        coefficients = (a, da, 3 * (b - a) - 2 * da - db, -2 * (b - a) + da + db)
        lines.append('  {' + ', '.join(c.hex() + 'f' for c in coefficients) + '},\n')
    lines.append('};\n}\n')
    return ''.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = Path(__file__).parent / 'native/world_activation_tables.h'
    content = generate()
    if args.check:
        if path.read_text() != content:
            raise SystemExit('Activation table mismatch')
        print('Activation table matches')
    else:
        path.write_text(content)
