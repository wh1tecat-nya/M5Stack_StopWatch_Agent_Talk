#!/usr/bin/env python3
"""予測器の揺れを成分交換で調べる。全交換枝は診断専用。"""
import argparse
import json
from pathlib import Path
import wave

import numpy as np
import torch

from train_world import load_data, load_model, digest, write_json
from world_vocoder import synthesize, FRAME_MS, SR
from world_pitch_diagnosis import component_pairs, pitch_metrics, CONDITIONS
from compare_band_results import save_previews

CHECKPOINT_SHA = '6ca55af5d58e0ee2d00572483afa946a8325dd4bbee0676f2a71fe836daa9eed'
APPROVED_SHA = '3bd909da60c80be8a36b4ea13e6784c7574cc893088e62e9aaaf7bfde8b63889'


def run(args):
    torch.set_num_threads(2)
    checkpoint = args.result / 'output/best.pt'
    if digest(checkpoint) != CHECKPOINT_SHA:
        raise ValueError('前回の保存モデルではありません')
    audit = json.loads((args.result / 'analysis/audit.json').read_text())
    if not audit['integrity_passed'] or audit['checkpoint_sha256'] != CHECKPOINT_SHA:
        raise ValueError('保存モデルの照合が必要です')
    for name, expected in json.loads((args.result / 'output/run.json').read_text())['code_sha256'].items():
        if digest(Path(__file__).with_name(name)) != expected:
            raise ValueError('前回と推論コードが異なります')
    if digest(args.reference / 'world_compact_parameters.wav') != APPROVED_SHA:
        raise ValueError('暫定合格の基準音声が異なります')
    data = load_data(args.data)
    model, _ = load_model(checkpoint)
    args.output.mkdir(parents=True, exist_ok=False)
    reports = {'code_sha256': digest(__file__), 'diagnosis_code_sha256': digest(Path(__file__).with_name('world_pitch_diagnosis.py')),
               'checkpoint_sha256': CHECKPOINT_SHA, 'frame_period_ms': FRAME_MS, 'per_split': {}}
    reply = None
    for split in ('validation', 'test'):
        rows = []
        for item in data[split]:
            with torch.no_grad():
                features, f0, _ = model.physical(item['z'][None])
            pf, pp = features[0].numpy(), f0[0, 0].numpy()
            rf = item['features'].numpy()
            rp = np.exp(item['log_f0'][0].numpy()) * item['uv'][0].numpy()
            metrics = pitch_metrics(rp, pp)
            rows.append({'id': item['id'], **metrics})
            if item['id'] == 'mf07-test_reply': reply = (rf, rp, pf, pp, item['samples'])
        keys = [k for k in rows[0] if k != 'id']
        averages = {k: float(np.mean([r[k] for r in rows if r[k] is not None])) for k in keys}
        reports['per_split'][split] = {'rows': rows, 'mean_per_utterance': averages}
    if reply is None: raise ValueError('比較文がありません')
    rf, rp, pf, pp, samples = reply
    # 試聴は前回に使った倍精度F0をそのまま用い、学習用log変換の差を排除する。
    with np.load(args.reference / 'analysis_parameters.npz', allow_pickle=False) as reference:
        np.testing.assert_array_equal(reference['features'], rf)
        np.testing.assert_array_equal(reference['uv'][0] > 0, rp > 0)
        rp = reference['f0'].copy()
    pairs = component_pairs(rf, rp, pf, pp)
    audios = {name: synthesize(features, pitch, samples) for name, (features, pitch) in pairs.items()}
    original_prediction = np.load(args.result / 'analysis/mf07-test_reply.npy', allow_pickle=False)
    np.testing.assert_array_equal(audios['model_all'], original_prediction)
    for name, y in audios.items():
        if len(y) != samples or not np.isfinite(y).all(): raise ValueError('再合成波形が不正です')
        np.save(args.output / (name + '.npy'), y, allow_pickle=False)
    save_previews(args.output / 'listening', audios, {}, pcm_scale=32768)
    stats = {}
    for name in audios:
        path = args.output / 'listening' / (name + '.wav')
        with wave.open(str(path)) as wav:
            if (wav.getnframes(), wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (282240, SR, 1, 2):
                raise ValueError('試聴WAVの形式が不正です')
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
        stats[name] = {'sha256': digest(path), 'rms': float(np.sqrt(np.mean((pcm.astype(float) / 32768) ** 2))),
                       'rails': int(np.count_nonzero((pcm == 32767) | (pcm == -32768)))}
        if stats[name]['rails']: raise ValueError('PCM上限へ到達しています')
    if np.ptp([r['rms'] for r in stats.values()]) > 5e-7:
        raise ValueError('試聴WAVの平均RMSが一致しません')
    # 元の暫定合格音声を再現することも別途検査する。保存時はRMS 0.1だった。
    with wave.open(str(args.reference / 'world_compact_parameters.wav')) as wav:
        approved_pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
    teacher = audios['teacher_all']
    teacher_gain = .1 / float(np.sqrt(np.mean(teacher ** 2)))
    np.testing.assert_array_equal(np.rint(teacher * teacher_gain * 32768).astype('<i2'), approved_pcm)
    reports.update(conditions={name: {'timbre': a, 'pitch_and_voicing': b} for name, (a, b) in CONDITIONS.items()},
                   listening=stats, learned_output_unchanged=True, approved_resynthesis_reproduced=True,
                   new_training_performed=False, firmware_changed=False, diagnostic_only=True,
                   deployable_candidate=False, quality_approved=False,
                   limitations=['交換音声は教師成分を使う診断であり、そのまま実機へ採用できない。',
                                '音程側はF0と有声判定、音色側はスペクトル包絡と非周期成分をまとめて扱う。',
                                '成分間の依存があるため、単一原因を確定する実験ではない。',
                                'ピッチ誤差は両者が有声の区間、隣接差分は隣接2フレームとも両者が有声の区間に限る。',
                                '教師F0は推定値であり正解とは限らない。平均RMS一致でありLUFS一致ではない。'])
    write_json(args.output / 'report.json', reports)
    print(json.dumps({'per_split': {k: v['mean_per_utterance'] for k, v in reports['per_split'].items()},
                      'wav_validation': stats, 'approved_resynthesis_reproduced': True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--result', type=Path, required=True)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--reference', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    run(p.parse_args())
