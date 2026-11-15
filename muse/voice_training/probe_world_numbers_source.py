#!/usr/bin/env python3
"""数の比較文について、Irodori元音声・再合成・特徴圧縮を切り分ける。"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from train_world import DATASET_SHA, digest, write_json
from probe_world import read_pcm
from world_vocoder import SR, analyze, encode, synthesize, synthesize_full
from prepare_world_envelope_baseline import waveform_stats
from compare_band_results import save_previews
from audit_world_training import pcm

SAMPLE_ID = 'mf07-expand_test_03'
SOURCE_SHA = '67940777dcff1cd16b8ea4246e8630c88cea3110de7405ece6c54cbe9d74af3f'


def run(args):
    if digest(args.features / 'dataset.json') != DATASET_SHA:
        raise ValueError('学習時と同じ特徴データではありません')
    dataset = json.loads((args.features / 'dataset.json').read_text())
    feature_row = next(r for r in dataset['rows'] if r['id'] == SAMPLE_ID)
    source_row = next(r for r in json.loads((args.source / 'dataset.json').read_text())
                      if r['sample_id'] == SAMPLE_ID)
    plan = next(r for r in json.loads(Path(__file__).with_name('expanded_texts.json').read_text())
                if 'mf07-' + r['id'] == SAMPLE_ID)
    if (source_row['text'] != plan['text'] or source_row['split'] != plan['split']
            or source_row['profile'] != 'mf07'):
        raise ValueError('比較文または教師モデルが異なります')
    path = (args.source / source_row['wav']).resolve()
    if (not path.is_relative_to(args.source.resolve()) or digest(path) != SOURCE_SHA
            or source_row['wav_sha256'] != SOURCE_SHA or feature_row['source_wav_sha256'] != SOURCE_SHA):
        raise ValueError('元音声の出所が一致しません')
    stored = read_pcm(path, 48000)
    gain = source_row.get('storage_gain', 1.)
    if not np.isfinite(gain) or not .245 <= gain <= 1:
        raise ValueError('保存ゲインが不正です')
    restored = stored.copy()
    restored /= gain
    original = resample_poly(restored, 147, 320).astype(np.float32)
    if len(original) != feature_row['samples'] or len(original) != 109368:
        raise ValueError('比較音声の長さが異なります')
    feature_path = args.features / feature_row['file']
    if digest(feature_path) != feature_row['sha256']:
        raise ValueError('特徴ファイルが一致しません')
    f0, sp, ap = analyze(original)
    features, log_f0, uv = encode(f0, sp, ap)
    with np.load(feature_path, allow_pickle=False) as saved:
        for key, values in {'features': features, 'log_f0': log_f0, 'uv': uv}.items():
            np.testing.assert_array_equal(values, saved[key])
    # 音程の丸めまで揃え、圧縮あり・なしの差を包絡と非周期成分に限定する。
    common_f0 = np.exp(log_f0[0]) * uv[0]
    audios = {
        'irodori_original_22050': original,
        'world_uncompressed': synthesize_full(common_f0, sp, ap, len(original)),
        'world_compact_reference': synthesize(features, common_f0, len(original)),
    }
    previous = json.loads((args.previous / 'report.json').read_text())
    old_preview = json.loads((args.previous / 'listening/preview.json').read_text())
    previous_path = args.previous / 'listening/teacher_all.wav'
    previous_pcm = pcm(previous_path, previous['wav_validation']['teacher_all']['sha256'])
    previous_gain = old_preview['files']['teacher_all']['gain']
    np.testing.assert_array_equal(
        np.rint(audios['world_compact_reference'] * previous_gain * 32768).astype('<i2'),
        np.rint(previous_pcm * 32768).astype('<i2'))
    args.output.mkdir(parents=True, exist_ok=False)
    save_previews(args.output / 'listening', audios, {}, pcm_scale=32768)
    stats = {name: waveform_stats(args.output / 'listening' / (name + '.wav')) for name in audios}
    if any(s['rails'] for s in stats.values()) or np.ptp([s['rms'] for s in stats.values()]) > 5e-7:
        raise ValueError('試聴音声の音量またはPCM範囲が不正です')
    # 元の48kHzも保存音声のまま参照できるようにする。試聴3本は同じ22.05kHz。
    report = {
        'sample_id': SAMPLE_ID, 'text': source_row['text'], 'source_wav': str(path),
        'source_sha256': SOURCE_SHA, 'source_storage_gain': gain,
        'source_contains_generation_watermark': source_row.get('watermark_applied'),
        'feature_dataset_sha256': DATASET_SHA, 'feature_arrays_reproduced': True,
        'previous_reference_pcm_reproduced': True, 'resampling': '48000 to 22050 Hz, resample_poly(147, 320)',
        'same_f0_for_both_resynthesis_conditions': True,
        'waveforms': stats,
        'source_code_sha256': {n: digest(Path(__file__).with_name(n)) for n in
            ('probe_world_numbers_source.py', 'world_vocoder.py', 'compare_band_results.py')},
        'diagnostic_only': True, 'new_training_performed': False,
        'weights_changed': False, 'firmware_changed': False, 'quality_approved': False,
        'limitations': [
            '元音声は生成時に保存された教師音声です。人間の録音や品質合格済み音声ではありません。',
            '元音声も他の2本と同じ22.05kHzへ変換しています。音声再合成や音程変更はしていません。',
            '圧縮なしもWORLDの分析・再合成を通ります。Irodoriそのものの出力ではありません。',
            '基準③にも残るかは追加確認中です。直前の「どれも変わらず」を③の評価と断定しません。',
            '平均RMS一致でありLUFS一致ではありません。任意文章TTSと実機動作は未完成です。',
        ],
    }
    write_json(args.output / 'report.json', report)
    print(json.dumps({'feature_arrays_reproduced': True, 'previous_reference_pcm_reproduced': True,
                      'waveforms': stats}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'features', 'previous', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
