#!/usr/bin/env python3
"""固定した補正版の残差を計測し、短い比較文で成分を個別に交換する。"""
import argparse
import json
from pathlib import Path

import numpy as np
import torch

from train_world import load_data, digest, write_json
from world_envelope_export import restore
from world_envelope_loss import EnvelopeLoss
from world_pitch_diagnosis import component_pairs, pitch_metrics
from world_timbre_diagnosis import components
from world_vocoder import synthesize
from prepare_world_envelope_baseline import CHECKPOINT_SHA, SELECTION, waveform_stats
from compare_band_results import save_previews


@torch.no_grad()
def run(args):
    torch.set_num_threads(2)
    baseline = json.loads((args.baseline / 'baseline.json').read_text())
    if digest(args.baseline / 'baseline.pt') != CHECKPOINT_SHA:
        raise ValueError('聴取した基準モデルではありません')
    model, metadata = restore(args.baseline / 'fp32')
    if metadata != baseline['export']:
        raise ValueError('照合済みの書き出しと異なります')
    loss = EnvelopeLoss()
    selected = {sid for _, sid in SELECTION}
    rows, audios = [], {}
    for item in load_data(args.data)['test']:
        if item['id'] not in selected:
            continue
        z = item['z'][None]
        output = model(z)
        features, f0, _ = model.physical(z)
        predicted, pitch = features[0].numpy(), f0[0, 0].numpy()
        teacher = item['features'].numpy()
        teacher_pitch = np.exp(item['log_f0'][0].numpy()) * item['uv'][0].numpy()
        wave = synthesize(predicted, pitch, item['samples'])
        saved = args.result / 'analysis' / ('focused_' + item['id'] + '.npy')
        np.testing.assert_array_equal(wave, np.load(saved, allow_pickle=False))
        weight = loss.frame_weights(item['features'][None], item['uv'][None])
        predicted_gate = features[:, 60:].clamp(-60, -.001).mean(1) <= -.5
        teacher_gate = item['features'][None, 60:].clamp(-60, -.001).mean(1) <= -.5
        rows.append({
            'id': item['id'], 'frames': z.shape[-1], 'raw_waveform_reproduced': True,
            'pitch': pitch_metrics(teacher_pitch, pitch),
            'ap_gate_mismatch_frames': int((predicted_gate != teacher_gate).sum()),
            'ap_gate_mismatch_teacher_voiced_frames': int(
                ((predicted_gate != teacher_gate) & (item['uv'] > 0)).sum()),
            'envelope': loss.diagnostics(output, item['features'][None], model, weight),
        })
        # 文章はユーザーが残差を指摘した短い二文目に固定する。
        if item['id'] != 'mf07-expand_test_03':
            continue
        timbres = components(teacher, predicted, pitch)
        pitches = component_pairs(teacher, teacher_pitch, predicted, pitch)
        pairs = {name: timbres[name] for name in
                 ('model_all', 'teacher_envelope', 'teacher_aperiodicity')}
        pairs['teacher_pitch_voicing'] = pitches['teacher_pitch_model_timbre']
        pairs['teacher_all'] = pitches['teacher_all']
        for name, (values, frequencies) in pairs.items():
            audios[name] = synthesize(values, frequencies, item['samples'])
        np.testing.assert_array_equal(audios['model_all'], wave)
        # 全文を合成する。語の境界を推測して切り取ったり、接合したりしない。
    if {r['id'] for r in rows} != selected or len(audios) != 5:
        raise ValueError('比較対象が不足しています')
    if any(y.shape != (109368,) or not np.isfinite(y).all() for y in audios.values()):
        raise ValueError('二文目の波形長または値が不正です')
    args.output.mkdir(parents=True, exist_ok=False)
    save_previews(args.output / 'listening', audios,
                  {'checkpoint_sha256': CHECKPOINT_SHA}, pcm_scale=32768)
    stats = {name: waveform_stats(args.output / 'listening' / (name + '.wav'))
             for name in audios}
    if any(r['rails'] for r in stats.values()) or np.ptp([r['rms'] for r in stats.values()]) > 5e-7:
        raise ValueError('比較音声の平均音量またはPCM範囲が不正です')
    report = {
        'checkpoint_sha256': CHECKPOINT_SHA,
        'weights_sha256': digest(args.baseline / 'fp32/weights.f32'),
        'source_code_sha256': {name: digest(Path(__file__).with_name(name)) for name in
            ('probe_world_envelope_residual.py', 'world_timbre_diagnosis.py',
             'world_pitch_diagnosis.py', 'world_vocoder.py', 'world_envelope_export.py',
             'world_envelope_loss.py', 'world_acoustic_loss.py', 'compare_band_results.py')},
        'rows': rows,
        'listening_text': '十六個の箱を、四つずつ並べるとどうなるかな。',
        'listening_id': 'mf07-expand_test_03',
        'conditions': {
            'model_all': {'envelope': 'predicted', 'aperiodicity': 'predicted', 'pitch_and_voicing': 'predicted'},
            'teacher_envelope': {'envelope': 'teacher', 'aperiodicity': 'predicted', 'pitch_and_voicing': 'predicted'},
            'teacher_aperiodicity': {'envelope': 'predicted', 'aperiodicity': 'teacher', 'pitch_and_voicing': 'predicted'},
            'teacher_pitch_voicing': {'envelope': 'predicted', 'aperiodicity': 'predicted', 'pitch_and_voicing': 'teacher'},
            'teacher_all': {'envelope': 'teacher', 'aperiodicity': 'teacher', 'pitch_and_voicing': 'teacher'},
        },
        'wav_validation': stats,
        'diagnostic_only': True, 'weights_changed': False, 'firmware_changed': False,
        'quality_approved': False,
        'limitations': [
            '数値は一文全体。音素と時刻の対応は未確定で、指摘された語の原因を示すものではありません。',
            '成分を戻した音声は教師依存の診断で、学習モデルの改善版ではありません。',
            '教師特徴にも分析誤差があり、成分間の依存もあるため単独原因を断定しません。',
            '同じ平均RMSであり、LUFSの一致ではありません。',
            'モデルの入力は教師codec潜在表現です。任意文章TTSや実機性能は未完成・未検証です。',
        ],
    }
    write_json(args.output / 'report.json', report)
    print(json.dumps({'rows': rows, 'wav_validation': stats}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'result', 'data', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
