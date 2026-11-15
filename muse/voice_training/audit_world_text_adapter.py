#!/usr/bin/env python3
"""試作接続器の固定時刻音声を生成し、同じ音素認識器で発音を比較する。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from scipy.io import wavfile

from align_world_text import edit_distance


def run(args):
    import onnxruntime as ort
    ort.disable_telemetry_events()
    cache = Path(__file__).resolve().parents[1] / '.cache'
    root = cache / 'irodori-text-frontend-20261114'
    native = cache / 'irodori-realtime-20261114/output16k-host'
    weights = cache / 'irodori-world-envelope-baseline-20261113/fp32/weights.f32'
    if hashlib.sha256(weights.read_bytes()).hexdigest() != '75c2b800975f31b5204b926df5f1cef6eaabcd6ee5f684d8a66c9a19d194382b':
        raise ValueError('Accepted voice weights changed')
    model = cache / 'japanese-phoneme-aligner'
    if hashlib.sha256((model / 'model_quantized.onnx').read_bytes()).hexdigest() != 'ad29e980cb0f50044854bf25324506ace4ff0c48b98b4d194953028236c3ab6f':
        raise ValueError('Alignment model changed')
    vocab = json.loads((model / 'vocab.json').read_text())
    blank = {vocab[k] for k in ['PAD', 'pau', 'sil']}
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model / 'model_quantized.onnx'), sess_options=options,
                                   providers=['CPUExecutionProvider'])
    rows = json.loads((cache / 'irodori-world-features-20261112-final/dataset.json').read_text())['rows']
    args.output.mkdir(parents=True, exist_ok=False)
    report = []
    for key in args.ids:
        if Path(key).name != key:
            raise ValueError('Unsafe utterance ID')
        row = next(r for r in rows if r['id'] == key)
        frames = row['frames']
        if args.direct_world:
            physical = args.pilot / (key + '.physical.f32')
            values = np.fromfile(physical, dtype='<f4')
            if values.size != frames * 64 or not np.isfinite(values).all():
                raise ValueError('Invalid direct acoustic output')
        else:
            z = np.load(args.pilot / (key + '.predicted_latent.npy'), allow_pickle=False)
            if z.ndim != 2 or z.shape[0] != 32 or not np.isfinite(z).all():
                raise ValueError('Invalid predicted latent')
            x = np.stack([np.interp(np.arange(frames) * .005, np.arange(z.shape[1]) * .04, channel)
                          for channel in z]).astype('<f4')
            input_path = args.output / (key + '.input.f32'); x.tofile(input_path)
            features = args.output / (key + '.features.f32')
            subprocess.run([str(native / 'world_envelope_cli'), str(weights), str(input_path),
                            str(features), '384', '1', 'tiled'], check=True, capture_output=True)
            physical = Path(str(features) + '.physical.f32')
        samples = (frames - 1) * 16000 // 200
        wave = args.output / (key + '.wave.f32')
        subprocess.run([str(native / 'world_synthesis_cli'), str(physical),
                        str(wave), str(samples), '256', '1'], check=True, capture_output=True)
        alignment = json.loads((root / 'all-utterances' / key / 'alignment.json').read_text())
        target = [vocab[p['phone']] for p in alignment['phonemes']]
        result = {'id': key, 'seconds': samples / 16000}
        for name, path in [('baseline', cache / 'irodori-realtime-20261114/output16k-audit' / (key + '.block256.f32')),
                           ('adapter', wave)]:
            audio = np.fromfile(path, dtype='<f4')
            if len(audio) != samples or not np.isfinite(audio).all():
                raise ValueError('Invalid comparison waveform')
            audio *= min(.1 / max(float(np.sqrt(np.mean(audio * audio))), 1e-6),
                         .95 / max(float(np.max(np.abs(audio))), 1e-6))
            pcm = np.rint(audio * 32767).astype(np.int16)
            wavfile.write(args.output / (key + '.' + name + '.wav'), 16000, pcm)
            labels = session.run(['logits'], {'input_values': pcm.astype(np.float32)[None] / 32768})[0][0].argmax(-1)
            predicted = [int(v) for i, v in enumerate(labels) if (i == 0 or v != labels[i-1]) and v not in blank]
            result[name + '_phone_error_rate'] = edit_distance(predicted, target) / len(target)
        report.append(result)
        print(json.dumps(result), flush=True)
    (args.output / 'report.json').write_text(json.dumps({
        'experimental_only': True, 'teacher_timing_used': True, 'device_integrated': False,
        'recognizer_proxy_not_listening_approval': True, 'rows': report}, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ids', nargs='+', default=['mf07-expand_test_03', 'mf07-expand_test_06'])
    parser.add_argument('--direct-world', action='store_true')
    run(parser.parse_args())
