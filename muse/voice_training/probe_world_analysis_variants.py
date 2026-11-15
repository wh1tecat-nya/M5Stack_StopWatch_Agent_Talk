#!/usr/bin/env python3
"""元音声から分析方式を変えて再合成し、語尾の違和感を診断する。"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyworld as pw
from scipy.signal import resample_poly

from train_world import digest, write_json
from probe_world import read_pcm
from probe_world_numbers_source import SOURCE_SHA, SAMPLE_ID
from world_analysis_variants import CONDITIONS, analyze_variant, voicing_stats
from world_vocoder import synthesize_full, encode
from prepare_world_envelope_baseline import waveform_stats
from compare_band_results import save_previews
from audit_world_training import pcm


def run(args):
    previous = json.loads((args.previous / 'report.json').read_text())
    source = Path(previous['source_wav'])
    if (previous['source_sha256'] != SOURCE_SHA or previous['sample_id'] != SAMPLE_ID
            or digest(source) != SOURCE_SHA):
        raise ValueError('直前の教師音声と異なります')
    original48 = read_pcm(source, 48000)
    gain = previous['source_storage_gain']
    if not np.isfinite(gain) or not .245 <= gain <= 1:
        raise ValueError('保存ゲインが不正です')
    original48 /= gain
    original = resample_poly(original48, 147, 320).astype(np.float32)
    if len(original) != 109368:
        raise ValueError('数の比較文と長さが異なります')
    old_preview = json.loads((args.previous / 'listening/preview.json').read_text())
    audios, analyses, stats = {'irodori_original': original}, {}, {}
    for name in CONDITIONS:
        f0, sp, ap = analyze_variant(original, name)
        # 直前の比較と同じ音程のfloat32保存形式を使用する。
        _, log_f0, uv = encode(f0, sp, ap)
        physical_f0 = np.exp(log_f0[0]) * uv[0]
        audios[name] = synthesize_full(physical_f0, sp, ap, len(original))
        analyses[name] = (f0, sp, ap)
        stats[name] = {
            'full': voicing_stats(f0, ap),
            'last_second': voicing_stats(f0[-200:], ap[-200:]),
        }
    np.testing.assert_array_equal(analyses['current_dio'][0], analyses['dio_without_ap_gate'][0])
    np.testing.assert_array_equal(analyses['current_dio'][1], analyses['dio_without_ap_gate'][1])
    for current, old in (('irodori_original', 'irodori_original_22050'), ('current_dio', 'world_uncompressed')):
        path = args.previous / 'listening' / (old + '.wav')
        saved = pcm(path, previous['waveforms'][old]['sha256'])
        np.testing.assert_array_equal(
            np.rint(audios[current] * old_preview['files'][old]['gain'] * 32768).astype('<i2'),
            np.rint(saved * 32768).astype('<i2'))
    args.output.mkdir(parents=True, exist_ok=False)
    save_previews(args.output / 'listening', audios, {}, pcm_scale=32768)
    wavs = {name: waveform_stats(args.output / 'listening' / (name + '.wav')) for name in audios}
    if any(s['rails'] for s in wavs.values()) or np.ptp([s['rms'] for s in wavs.values()]) > 5e-7:
        raise ValueError('平均音量またはPCM範囲が不正です')
    for name, (f0, sp, ap) in analyses.items():
        np.savez_compressed(args.output / (name + '_parameters.npz'), f0=f0, sp=sp, ap=ap)
    report = {
        'sample_id': SAMPLE_ID, 'source_sha256': SOURCE_SHA, 'pyworld': pw.__version__,
        'source_probe_report_sha256': digest(args.previous / 'report.json'),
        'conditions': CONDITIONS, 'all_conditions_use_stonemask': True,
        'full_resolution_parameters': True,
        'previous_original_and_resynthesis_pcm_reproduced': True,
        'gate_comparison_f0_and_spectral_envelope_identical': True,
        'analysis_statistics': stats, 'wav_validation': wavs,
        'code_sha256': {n: digest(Path(__file__).with_name(n)) for n in
            ('world_analysis_variants.py', 'probe_world_analysis_variants.py', 'world_vocoder.py',
             'compare_band_results.py', 'test_world_analysis_variants.py')},
        'references': [
            'https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder/blob/master/pyworld/pyworld.pyx',
            'https://github.com/mmorise/World/blob/master/src/d4c.cpp',
            'https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder/blob/master/demo/demo.py',
        ],
        'diagnostic_only': True, 'weights_changed': False, 'new_training_performed': False,
        'firmware_changed': False, 'quality_approved': False,
        'limitations': [
            '分析パラメータは教師音声から求めています。小型モデルの改善結果ではありません。',
            '追加の有声判定を外すと不要な周期音が出る可能性があり、聴感確認なしで採用しません。',
            'Harvest条件は音程推定に加え、その音程で包絡と非周期成分を再分析します。',
            '末尾1秒は単語境界を確定した区間ではありません。判定回数は音質の点数ではありません。',
            '教師声質の問題や吃音とは断定しません。音声の分析・再合成による変化を比較します。',
            '平均RMS一致でありLUFS一致ではありません。実機動作と任意文章TTSは未完成です。',
        ],
    }
    write_json(args.output / 'report.json', report)
    print(json.dumps({'analysis_statistics': stats, 'wav_validation': wavs}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
