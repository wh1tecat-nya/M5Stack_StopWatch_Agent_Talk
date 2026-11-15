#!/usr/bin/env python3
"""2条件の同一更新回数の比較と、音量を揃えた試聴音声を保存する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

import numpy as np
import torch
from diagnose_decoder import compare


def save_json(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def save_previews(folder, audios, checkpoint_steps, *, pcm_scale=32767):
    if pcm_scale not in (32767, 32768):
        raise ValueError('PCMのスケールが不正です')
    folder.mkdir(parents=True, exist_ok=False)
    # RMSを揃える。クリップを避けるため、共通の目標RMSを全音声のピークから決める。
    rms = {name: float(np.sqrt(np.mean(audio ** 2))) for name, audio in audios.items()}
    target_rms = min(.1, *[.90 * rms[name] / max(float(np.max(np.abs(audio))), 1e-9) for name, audio in audios.items()])
    preview = {'normalization': 'equal full-utterance RMS; not LUFS', 'target_rms': target_rms,
               'pcm_scale': pcm_scale,
               'checkpoint_steps': checkpoint_steps, 'files': {}}
    for name, audio in audios.items():
        gain = target_rms / max(rms[name], 1e-9)
        # 既存比較の再現用に32767を既定値として維持する。PCM入力と新規波形を
        # 混ぜる比較では、読み込みと一致する32768を明示して丸めの偏りを避ける。
        pcm = np.rint(audio * gain * pcm_scale).astype('<i2')
        path = folder / (name + '.wav')
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(22050); wav.writeframes(pcm.tobytes())
        preview['files'][name] = {'gain': gain, 'source_rms': rms[name], 'peak': float(np.max(np.abs(audio*gain))),
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    save_json(folder / 'preview.json', preview)


def main(result, trusted, output):
    # 実行入力はaudit_reconstruction.pyで両条件とも照合してから実行する。
    for condition in ('baseline', 'bands'):
        if not json.loads((result / f'audit_{condition}.json').read_text())['integrity_passed']:
            raise ValueError('結果の照合が完了していません')
    sys.path.insert(0, str(trusted.resolve()))
    import train_decoder as training
    torch.set_num_threads(2)
    data = training.load_data(trusted / 'data')
    output.mkdir(parents=True, exist_ok=False)
    reports = {'analysis_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'diagnostic_code_sha256': hashlib.sha256(Path(__file__).with_name('diagnose_decoder.py').read_bytes()).hexdigest(),
               'conditions': {}, 'perceptual_quality_verified': False}
    endpoints = {}
    for condition in ('baseline', 'bands'):
        run = json.loads((result / condition / 'run.json').read_text())
        complete = json.loads((result / condition / 'completed.json').read_text())
        ckpt = torch.load(result / condition / 'endpoint.pt', map_location='cpu', weights_only=True)
        if ckpt['step'] != 2000 or run['steps_requested'] != 2000:
            raise ValueError('同じ更新回数の比較になっていません')
        model = training.Reconstruction(*training.normalization(data['train']))
        model.load_state_dict(ckpt['model'], strict=True); model.eval()
        with torch.no_grad():
            score, rows = training.evaluate(model, data['validation'], 'cpu')
            endpoints[condition] = {item['row']['sample_id']: model.eval()(item['z'][None])[0, :item['samples']].numpy()
                                    for item in data['test']}
        reports['conditions'][condition] = {'run': run, 'selected_checkpoint': complete,
                                           'endpoint_checkpoint_sha256': hashlib.sha256((result / condition / 'endpoint.pt').read_bytes()).hexdigest(),
                                           'endpoint_validation_loss': score, 'endpoint_validation_rows': rows,
                                           'endpoint_test_diagnostics': {}, 'selected_test_diagnostics': {}}
    for item in data['test']:
        sample_id = item['row']['sample_id']
        reference = item['y'][:item['samples']].numpy()
        audios = {'reference': reference}
        for condition in ('baseline', 'bands'):
            raw = np.load(result / condition / sample_id / 'reconstruction_float32.npy', allow_pickle=False)
            audios[condition] = raw
            reports['conditions'][condition]['selected_test_diagnostics'][sample_id] = compare(reference, raw)
            reports['conditions'][condition]['endpoint_test_diagnostics'][sample_id] = compare(reference, endpoints[condition][sample_id])
        save_previews(output / 'listening_selected' / sample_id, audios,
                      {c: reports['conditions'][c]['selected_checkpoint']['best_step'] for c in endpoints})
        save_previews(output / 'listening_endpoint' / sample_id,
                      {'reference': reference, **{c: endpoints[c][sample_id] for c in endpoints}},
                      {c: 2000 for c in endpoints})
    save_json(output / 'comparison.json', reports)
    print(json.dumps({c: {'best_step': r['selected_checkpoint']['best_step'],
                          'selected_validation': r['selected_checkpoint']['validation_loss'],
                          'endpoint_validation': r['endpoint_validation_loss']}
                      for c, r in reports['conditions'].items()}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--trusted-input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    main(args.result, args.trusted_input, args.output)
