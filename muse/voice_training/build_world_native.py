#!/usr/bin/env python3
"""固定したWORLDと移植コードをビルドする。依存ソースは.cache内のみ。"""
import argparse
from pathlib import Path
import shutil
import subprocess

from prepare_world_source import prepare
from prepare_world_float import prepare_float


def build(output, sanitize=False, target=False, fp32=False, dsp_fft=False, bounded_math=False, lut_math=False, fft_size=1024, compact=False, filter_stride=0, phase_recurrence=False, silu_table=False, sample_rate=22050):
    here = Path(__file__).resolve().parent
    source = prepare_float(bounded_math, lut_math=lut_math) if fp32 else prepare()
    output.mkdir(parents=True, exist_ok=True)
    compiler = 'c++'
    flags = ['-std=c++17', '-O2', '-ffp-contract=off', '-Wall', '-Wextra', '-Werror',
             '-Wno-missing-field-initializers', '-I' + str(source / 'src'), '-I' + str(here / 'native')]
    if fp32:
        flags += ['-DSUMI_WORLD_FP32=1']
    if dsp_fft:
        flags += ['-DSUMI_WORLD_FFT_DSP=1']
    if compact:
        flags += ['-DSUMI_WORLD_COMPACT_WORKSPACE=1']
    if bounded_math:
        flags += ['-DSUMI_WORLD_BOUNDED_MATH=1']
    if lut_math:
        flags += ['-DSUMI_WORLD_LUT_MATH=1']
    flags += [f'-DSUMI_WORLD_SYNTH_FFT_SIZE={fft_size}']
    flags += [f'-DSUMI_WORLD_SAMPLE_RATE={sample_rate}']
    if filter_stride:
        flags += [f'-DSUMI_WORLD_FILTER_STRIDE={filter_stride}']
    if phase_recurrence:
        flags += ['-DSUMI_WORLD_PHASE_RECURRENCE=1']
    if silu_table:
        flags += ['-DSUMI_WORLD_SILU_TABLE=1']
    if sanitize:
        flags[1] = '-O1'
        flags += ['-g', '-fsanitize=address,undefined', '-fno-omit-frame-pointer']
    if target:
        compiler = str(here.parent / '.cache/idf-tools/tools/xtensa-esp-elf/esp-15.2.0_20251204/xtensa-esp-elf/bin/xtensa-esp32s3-elf-g++')
        flags += ['-fno-exceptions', '-fno-rtti', '-fstack-usage', '-ffunction-sections', '-fdata-sections']
    # codec.cpp and synthesis.cpp are included by world_stream.cpp to reuse
    # pinned internal routines with persistent state; never link them twice.
    units = [source / ('src/' + name + '.cpp') for name in ('common', 'fft', 'matlabfunctions')]
    if dsp_fft:
        units = [u for u in units if u.stem != 'fft'] + [here / 'native/world_fft_dsp.cpp']
    units += [here / 'native/world_stream.cpp', here / 'native/world_envelope.cpp']
    objects = []
    for unit in units:
        obj = output / (unit.stem + '.o')
        # Upstream has warning patterns not controlled by this project.
        local_flags = flags if unit.parent.name == 'native' else [f for f in flags if f != '-Werror']
        subprocess.run([compiler, *local_flags, '-c', str(unit), '-o', str(obj)], check=True)
        objects.append(str(obj))
    if not target:
        programs = ['world_synthesis_cli', 'test_world_stream', 'world_envelope_cli', 'test_world_envelope', 'test_world_feature_stream', 'test_world_playback']
        if dsp_fft:
            programs += ['test_world_fft_dsp']
        if bounded_math:
            programs += ['test_world_fast_math']
        if silu_table:
            programs += ['test_world_activation']
        for name in programs:
            unit = here / 'native' / (name + '.cpp')
            if unit.exists():
                subprocess.run([compiler, *flags, str(unit), *objects, '-o', str(output / name)], check=True)
    shutil.copy2(source / 'LICENSE.txt', output / 'WORLD-LICENSE.txt')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--target', action='store_true')
    parser.add_argument('--fp32', action='store_true')
    parser.add_argument('--dsp-fft', action='store_true', help='Host test of DSP FFT adapter conventions')
    parser.add_argument('--bounded-math', action='store_true', help='Explicit bounded math approximations; compiler fast-math remains off')
    parser.add_argument('--lut-math', action='store_true', help='Waveform-only float lookup experiment')
    parser.add_argument('--fft-size', type=int, choices=[256, 512, 1024], default=1024, help='Smaller synthesis size requires a new listening evaluation')
    parser.add_argument('--sample-rate', type=int, choices=[16000, 22050], default=22050)
    parser.add_argument('--compact', action='store_true', help='Smaller matrix and FFT workspace; real FFT 1024 remains supported')
    parser.add_argument('--filter-stride', type=int, choices=[0, 1, 2], default=0, help='Experimental frame-based filters; requires listening evaluation')
    parser.add_argument('--phase-recurrence', action='store_true', help='Re-anchored double-precision phase increments; audit waveform error separately')
    parser.add_argument('--silu-table', action='store_true', help='Experimental neural activation approximation; audit model and waveform separately')
    args = parser.parse_args()
    if args.target and args.sanitize:
        parser.error('target and sanitize are mutually exclusive')
    if args.dsp_fft and (not args.fp32 or args.target):
        parser.error('dsp-fft requires fp32 host build; firmware builds use IDF')
    if (args.bounded_math or args.lut_math) and not args.fp32:
        parser.error('math experiments require fp32')
    if args.fft_size != 1024 and not (args.fp32 and args.dsp_fft):
        parser.error('Smaller synthesis sizes require fp32 and dsp-fft')
    if args.fft_size == 256 and args.sample_rate != 16000:
        parser.error('256-point synthesis requires 16 kHz to retain the 65 Hz F0 range')
    if args.sample_rate != 22050 and not (args.fp32 and args.dsp_fft):
        parser.error('Output-rate experiment requires fp32 and dsp-fft')
    if args.compact and not args.dsp_fft:
        parser.error('Compact workspace requires dsp-fft')
    if args.filter_stride and not (args.fp32 and args.dsp_fft and args.lut_math):
        parser.error('Frame filters require fp32, dsp-fft and lut-math')
    print(build(args.output.resolve(), args.sanitize, args.target, args.fp32, args.dsp_fft, args.bounded_math, args.lut_math, args.fft_size, args.compact, args.filter_stride, args.phase_recurrence, args.silu_table, args.sample_rate))
