#!/usr/bin/env python3
"""残る語尾の揺れを、三成分の全組み合わせで診断する。"""
import argparse
import json
from pathlib import Path

import numpy as np
import torch

from train_world import load_data, digest, write_json
from world_envelope_export import restore
from world_pitch_diagnosis import component_pairs, pitch_metrics
from world_vocoder import synthesize
from prepare_world_envelope_baseline import CHECKPOINT_SHA, waveform_stats
from compare_band_results import save_previews
from audit_world_training import pcm

# 順序は包絡・非周期成分・音程（有声判定を含む）。Trueは教師を使用する。
FACTORS = {
    'model_all': (False, False, False),
    'teacher_envelope': (True, False, False),
    'teacher_aperiodicity': (False, True, False),
    'teacher_pitch_voicing': (False, False, True),
    'teacher_envelope_aperiodicity': (True, True, False),
    'teacher_envelope_pitch': (True, False, True),
    'teacher_aperiodicity_pitch': (False, True, True),
    'teacher_all': (True, True, True),
}


def combinations(teacher, teacher_f0, predicted, predicted_f0):
    # 既存の診断と同じ形状・有限値・時間軸の検査を適用する。
    validated = component_pairs(teacher, teacher_f0, predicted, predicted_f0)
    tf, tp = validated['teacher_all']
    pf, pp = validated['model_all']
    return {name: (np.concatenate(((tf if envelope else pf)[:60],
                                  (tf if aperiodicity else pf)[60:]), axis=0),
                   (tp if pitch else pp).copy())
            for name, (envelope, aperiodicity, pitch) in FACTORS.items()}


@torch.no_grad()
def run(args):
    torch.set_num_threads(2)
    baseline = json.loads((args.baseline / 'baseline.json').read_text())
    if digest(args.baseline / 'baseline.pt') != CHECKPOINT_SHA:
        raise ValueError('固定した基準モデルではありません')
    model, metadata = restore(args.baseline / 'fp32')
    if metadata != baseline['export']:
        raise ValueError('照合済みの重みではありません')
    previous = json.loads((args.previous / 'report.json').read_text())
    if previous['checkpoint_sha256'] != CHECKPOINT_SHA:
        raise ValueError('前回の試聴モデルと異なります')
    preview = json.loads((args.previous / 'listening/preview.json').read_text())
    item = next(i for i in load_data(args.data)['test'] if i['id'] == 'mf07-expand_test_03')
    f, p, _ = model.physical(item['z'][None])
    predicted, pitch = f[0].numpy(), p[0, 0].numpy()
    teacher = item['features'].numpy()
    teacher_pitch = np.exp(item['log_f0'][0].numpy()) * item['uv'][0].numpy()
    pairs = combinations(teacher, teacher_pitch, predicted, pitch)
    audios = {name: synthesize(values, frequencies, item['samples'])
              for name, (values, frequencies) in pairs.items()}
    for name, stats in previous['wav_validation'].items():
        path = args.previous / 'listening' / (name + '.wav')
        old = pcm(path, stats['sha256'])
        gain = preview['files'][name]['gain']
        np.testing.assert_array_equal(np.rint(audios[name] * gain * 32768).astype('<i2'),
                                      np.rint(old * 32768).astype('<i2'))
    if any(y.shape != (item['samples'],) or not np.isfinite(y).all() for y in audios.values()):
        raise ValueError('診断波形の長さまたは値が不正です')
    args.output.mkdir(parents=True, exist_ok=False)
    save_previews(args.output / 'listening', audios,
                  {'checkpoint_sha256': CHECKPOINT_SHA}, pcm_scale=32768)
    stats = {name: waveform_stats(args.output / 'listening' / (name + '.wav')) for name in audios}
    if any(v['rails'] for v in stats.values()) or np.ptp([v['rms'] for v in stats.values()]) > 5e-7:
        raise ValueError('平均音量またはPCM範囲が不正です')
    # 時刻の窓は語の境界を意味しない。末尾側の予測不一致を別途記録する。
    tail_rows = []
    model_gate = predicted[60:].clip(-60, -.001).mean(0) <= -.5
    teacher_gate = teacher[60:].clip(-60, -.001).mean(0) <= -.5
    for start in range(600, len(pitch) - 1, 40):
        stop = min(start + 40, len(pitch))
        if stop - start < 2:
            continue
        s = slice(start, stop)
        tail_rows.append({
            'start_seconds': start / 200, 'end_seconds': stop / 200,
            'pitch': pitch_metrics(teacher_pitch[s], pitch[s]),
            'ap_gate_mismatch_frames': int((model_gate[s] != teacher_gate[s]).sum()),
            'model_f0_voiced_but_ap_nonperiodic_frames': int(((pitch[s] > 0) & ~model_gate[s]).sum()),
            'teacher_f0_voiced_but_ap_nonperiodic_frames': int(((teacher_pitch[s] > 0) & ~teacher_gate[s]).sum()),
        })
    report = {
        'checkpoint_sha256': CHECKPOINT_SHA,
        'weights_sha256': digest(args.baseline / 'fp32/weights.f32'),
        'source_code_sha256': {name: digest(Path(__file__).with_name(name)) for name in
            ('probe_world_component_interactions.py', 'world_pitch_diagnosis.py',
             'world_vocoder.py', 'world_envelope_export.py', 'compare_band_results.py')},
        'previous_waveforms_reproduced': len(previous['wav_validation']),
        'conditions': {name: dict(zip(('envelope', 'aperiodicity', 'pitch_and_voicing'),
                                    ('teacher' if x else 'predicted' for x in factors)))
                       for name, factors in FACTORS.items()},
        'wav_validation': stats, 'tail_windows': tail_rows,
        'diagnostic_only': True, 'new_training_performed': False,
        'weights_changed': False, 'firmware_changed': False, 'quality_approved': False,
        'limitations': [
            '単一成分で残る違和感を調べる教師依存の組み合わせ診断です。改善モデルではありません。',
            '数値の時間窓を単語に対応付けていません。聴感の原因は試聴で別途確認します。',
            '成分間の依存と教師分析の誤差があり、一つの成分だけを原因と断定できません。',
            '全文RMSの一致であり、LUFSの一致ではありません。',
            '任意文章TTSとC152での動作は未完成・未検証です。',
        ],
    }
    write_json(args.output / 'report.json', report)
    print(json.dumps({'previous_waveforms_reproduced': report['previous_waveforms_reproduced'],
                      'wav_validation': stats, 'tail_windows': tail_rows}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'previous', 'data', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
