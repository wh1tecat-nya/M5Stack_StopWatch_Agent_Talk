#!/usr/bin/env python3
"""固定WORLDの演算部だけをFP32へ変換する。元ファイルとライセンスは保持する。"""
import hashlib
import json
from pathlib import Path
import re

from prepare_world_source import prepare

# A token-level rewrite avoids changing comments, strings, hexadecimal integer
# constants, symbol names or licenses. Generated third-party code stays in cache.
MATH = {'atan', 'ceil', 'cos', 'exp', 'fabs', 'floor', 'fmod', 'log', 'log10', 'pow', 'sin', 'sqrt'}
TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
                   r'\bdouble\b|\b(?:' + '|'.join(sorted(MATH)) + r')(?=\s*\()|'
                   r'(?<![\w.])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)[fFlL]?')


FAST_MATH = {'exp', 'log', 'log10', 'pow', 'sqrt', 'cos', 'sin'}


def convert(text, fast_math=False, math_profile=False, lut_math=False):
    def change(match):
        token = match.group()
        if token == 'double':
            return 'float'
        if token in MATH:
            if lut_math and token in FAST_MATH:
                return 'sumi_lut_' + token
            if math_profile and token in FAST_MATH:
                return 'sumi_profile_' + token
            # <math.h> in the embedded toolchain exposes double C functions.
            # Merely changing argument types still calls software double libm.
            return 'sumi_fast_' + token if fast_math and token in FAST_MATH else token + 'f'
        if token[0].isdigit() or token[0] == '.':
            return token if token[-1] in 'fFlL' else token + 'f'
        return token
    return TOKEN.sub(change, text)


def prepare_float(fast_math=False, math_profile=False, lut_math=False):
    source = prepare()
    if sum((fast_math, math_profile, lut_math)) > 1:
        raise ValueError('Profile the unchanged math implementation separately')
    root = source.with_name(source.name + ('-fp32-lut' if lut_math else '-fp32-profile' if math_profile else '-fp32-bounded' if fast_math else '-fp32'))
    hashes = {}
    for path in sorted(source.rglob('*')):
        if not path.is_file() or path.suffix not in ('.h', '.cpp'):
            continue
        relative = path.relative_to(source)
        text = convert(path.read_text(), fast_math, math_profile, lut_math)
        # WORLD's FFT sizes are powers of two. Their reciprocals are exactly
        # representable; hoisting these divisions avoids software __divsf3 in
        # every spectrum bin without changing normal finite results.
        if path.name == 'common.cpp':
            marker = '  float tmp;\n  for (int i = 0; i <= minimum_phase->fft_size / 2; ++i) {'
            divisor = ' /\n      minimum_phase->fft_size)'
            if text.count(marker) != 1 or text.count(divisor) != 3:
                raise ValueError('Pinned minimum-phase normalization changed')
            text = text.replace(marker, '  const float inverse_fft_size = 1.0f / minimum_phase->fft_size;\n' + marker)
            text = text.replace(divisor, ' * inverse_fft_size)')
            start = text.index('  minimum_phase->cepstrum[0][1] *= -1.0f;')
            end = text.index('  // Since x is complex number', start)
            original = text[start:end]
            # The mirrored log magnitude is real and even: its cepstrum is
            # real. Suppressing only numerical imaginary residue allows the
            # second transform to use the real FFT as well. Audited separately
            # from the unchanged FP64 oracle; no spectral bins are discarded.
            real = '''#ifdef SUMI_WORLD_FFT_DSP
  for (int i = 0; i <= minimum_phase->fft_size / 2; ++i)
    minimum_phase->log_spectrum[i] = minimum_phase->cepstrum[i][0] *
        (i == 0 || i == minimum_phase->fft_size / 2 ? 1.0f : 2.0f);
  for (int i = minimum_phase->fft_size / 2 + 1; i < minimum_phase->fft_size; ++i)
    minimum_phase->log_spectrum[i] = 0.0f;
  const auto real_forward = fft_plan_dft_r2c_1d(minimum_phase->fft_size,
      minimum_phase->log_spectrum, minimum_phase->minimum_phase_spectrum, FFT_ESTIMATE);
  fft_execute(real_forward);
  fft_destroy_plan(real_forward);
#else
'''
            text = text[:start] + real + original + '#endif\n\n' + text[end:]
        if path.name == 'codec.cpp':
            marker = '  float *mel_spectrum = new float[max_dimension + 2];'
            divisor = 'spectral_envelope[i] / max_dimension'
            if text.count(marker) != 1 or text.count(divisor) != 1:
                raise ValueError('Pinned codec normalization changed')
            text = text.replace(marker, '  const float inverse_dimension = 1.0f / max_dimension;\n' + marker)
            text = text.replace(divisor, 'spectral_envelope[i] * inverse_dimension')
        if path.name == 'synthesis.cpp':
            start = text.index('  for (int i = 0; i <= fft_size / 2; ++i) {', text.index('static void GetAperiodicResponse'))
            end = text.index('  fft_execute(inverse_real_fft->inverse_fft);', start)
            part = text[start:end]
            if part.count('minimum_phase->minimum_phase_spectrum[i][0]') != 2 or part.count('minimum_phase->minimum_phase_spectrum[i][1]') != 2:
                raise ValueError('Pinned noise-spectrum multiplication changed')
            part = part.replace('minimum_phase->minimum_phase_spectrum[i][0]', 'phase_re').replace('minimum_phase->minimum_phase_spectrum[i][1]', 'phase_im')
            part = part.replace('++i) {', '++i) {\n    const float phase_re = minimum_phase->minimum_phase_spectrum[i][0];\n    const float phase_im = minimum_phase->minimum_phase_spectrum[i][1];', 1)
            text = text[:start] + part + text[end:]
        if fast_math and 'sumi_fast_' in text:
            text = '#include "world_fast_math.h"\n' + text
        if math_profile and 'sumi_profile_' in text:
            text = '#include "world_math_profile.h"\n' + text
        if lut_math and 'sumi_lut_' in text:
            text = '#include "world_lut_math.h"\n' + text
            if path.name == 'synthesis.cpp':
                old = 'im2 = sumi_lut_sqrt(1.0f - re2 * re2);  // sin(pshift)'
                if text.count(old) != 1:
                    raise ValueError('Pinned fractional-shift formula changed')
                # Computing sqrt(1-cos²) magnifies the tiny lookup error near
                # zero phase. Direct sin is equivalent for this [0, pi] range.
                text = text.replace(old, 'im2 = fabsf(sumi_lut_sin(coefficient * i));  // stable sin(pshift)')
        data = text.encode()
        out = root / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists() or out.read_bytes() != data:
            out.write_bytes(data)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'LICENSE.txt').write_bytes((source / 'LICENSE.txt').read_bytes())
    (root / 'transformation.json').write_text(json.dumps({
        'source': source.name, 'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'operation': 'double types to float; decimal floating literals to float; C libm calls to explicit float variants; no fast-math',
        'bounded_math_helpers': fast_math,
        'sampled_math_profile': math_profile,
        'lookup_math_helpers': lut_math,
        'power_of_two_reciprocals_hoisted': True,
        'dsp_real_cepstrum_fft': True,
        'files': hashes}, indent=2) + '\n')
    return root


if __name__ == '__main__':
    print(prepare_float())
