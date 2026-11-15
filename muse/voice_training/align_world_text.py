#!/usr/bin/env python3
"""本体の発音列と教師音声をCTCで対応づける。学習採用は別途確認する。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ctc_path(log_probs, tokens, blank=0):
    """Global CTC Viterbi path; repeated labels require an intervening blank."""
    log_probs = np.asarray(log_probs, dtype=np.float64)
    tokens = np.asarray(tokens, dtype=np.int64)
    if (log_probs.ndim != 2 or not len(log_probs) or tokens.ndim != 1 or not len(tokens)
            or not np.isfinite(log_probs).all() or not 0 <= blank < log_probs.shape[1]
            or np.any(tokens < 0) or np.any(tokens >= log_probs.shape[1]) or np.any(tokens == blank)):
        raise ValueError('Invalid CTC input')
    if len(log_probs) < len(tokens) + np.count_nonzero(tokens[1:] == tokens[:-1]):
        raise ValueError('Too few frames for transcript')
    labels = np.full(2 * len(tokens) + 1, blank, dtype=np.int64)
    labels[1::2] = tokens
    skip = np.zeros(len(labels), dtype=bool)
    skip[2:] = (labels[2:] != blank) & (labels[2:] != labels[:-2])
    score = np.full(len(labels), -np.inf)
    score[:2] = log_probs[0, labels[:2]]
    trace = np.zeros((len(log_probs), len(labels)), dtype=np.uint8)
    for t in range(1, len(log_probs)):
        previous = np.stack([score, np.r_[-np.inf, score[:-1]], np.r_[-np.inf, -np.inf, score[:-2]]])
        previous[2, ~skip] = -np.inf
        trace[t] = np.argmax(previous, axis=0)
        score = previous[trace[t], np.arange(len(labels))] + log_probs[t, labels]
    state = len(labels) - (1 if score[-1] >= score[-2] else 2)
    if not np.isfinite(score[state]):
        raise ValueError('No complete alignment')
    path = np.empty(len(log_probs), dtype=np.int32)
    final_score = float(score[state])
    for t in range(len(path) - 1, -1, -1):
        path[t] = state
        state -= int(trace[t, state])
    if any(not np.any(path == 2 * i + 1) for i in range(len(tokens))):
        raise ValueError('Alignment omitted a phoneme')
    return path, final_score


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a):
        new = [i + 1]
        for j, y in enumerate(b):
            new.append(min(new[-1] + 1, row[j + 1] + 1, row[j] + (x != y)))
        row = new
    return row[-1]


def exported_phones(executable, dictionary, text_path, vocab):
    result = subprocess.run([str(executable.resolve()), str(dictionary.resolve()), str(text_path.resolve())],
                            check=True, capture_output=True, text=True, timeout=30)
    ids = []
    phones = []
    for line in result.stdout.splitlines():
        index, token = line.split('\t')
        ids.append({'id': int(index), 'token': token})
        phone = 'N' if token.startswith('N_') else token.lower() if token in ('A', 'E', 'O') else token
        if token in ('_', '^', '$', '?', '?!', '?.', '?~', '#', '[', ']'):
            continue
        if phone not in vocab:
            raise ValueError('Unsupported phone: ' + phone)
        phones.append({'phone': phone, 'token_index': len(ids) - 1})
    if not phones:
        raise ValueError('No phones in transcript')
    return ids, phones


def run(args):
    import onnxruntime as ort
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    ort.disable_telemetry_events()
    model = args.model / 'model_quantized.onnx'
    if sha(model) != 'ad29e980cb0f50044854bf25324506ace4ff0c48b98b4d194953028236c3ab6f':
        raise ValueError('Pinned alignment model changed')
    if sha(args.dictionary) != '284eb31a4699598ffe885d1623a7215199ff610163c34ded959b0baa6cc7c925':
        raise ValueError('Device dictionary changed')
    metadata = json.loads((args.model / 'source.json').read_text())
    for file in metadata['files']:
        if sha(args.model / file['file']) != file['sha256']:
            raise ValueError('Alignment metadata changed')
    vocab = json.loads((args.model / 'vocab.json').read_text())
    inverse_vocab = {v: k for k, v in vocab.items()}
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), options, providers=['CPUExecutionProvider'])
    dataset_path = args.data / 'dataset.json'
    dataset = json.loads(dataset_path.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for item in dataset:
        if args.ids and item['id'] not in args.ids:
            continue
        key = item['id']
        if Path(key).name != key:
            raise ValueError('Unsafe utterance ID')
        destination = args.output / key
        destination.mkdir()
        text_path = destination / 'text.txt'
        text_path.write_text(item['text'])
        record = {'id': key, 'split': item['split'], 'text': item['text'], 'training_approved': False}
        try:
            audio_path = (args.data / item['wav']).resolve()
            if not audio_path.is_relative_to(args.data.resolve()) or sha(audio_path) != item['wav_sha256']:
                raise ValueError('Teacher audio changed')
            ids, phones = exported_phones(args.executable, args.dictionary, text_path, vocab)
            sr, data = wavfile.read(audio_path)
            if data.ndim != 1 or not 8000 <= sr <= 96000:
                raise ValueError('Expected mono speech')
            if np.issubdtype(data.dtype, np.signedinteger):
                data = data.astype(np.float32) / (2 ** (np.iinfo(data.dtype).bits - 1))
            elif np.issubdtype(data.dtype, np.floating):
                data = data.astype(np.float32)
            else:
                raise ValueError('Unsupported WAV encoding')
            if not np.isfinite(data).all() or len(data) / sr > 60:
                raise ValueError('Invalid or excessively long audio')
            duration = len(data) / sr
            divisor = math.gcd(sr, 16000)
            audio = resample_poly(data, 16000 // divisor, sr // divisor).astype(np.float32)
            started = time.monotonic()
            logits = session.run(['logits'], {'input_values': audio[None]})[0][0]
            logits = logits.astype(np.float64)
            log_probs = logits - np.logaddexp.reduce(logits, axis=1)[:, None]
            np.save(destination / 'log_probs.npy', log_probs.astype(np.float32), allow_pickle=False)
            target = [vocab[p['phone']] for p in phones]
            # Permit actual pauses/silence between transcript phones. No
            # waveform is changed and no phoneme is invented to fill a gap.
            aligned_probs = log_probs.copy()
            silence = [vocab[name] for name in ['PAD', 'pau', 'sil']]
            aligned_probs[:, vocab['PAD']] = np.logaddexp.reduce(log_probs[:, silence], axis=1) - args.blank_penalty
            path, score = ctc_path(aligned_probs, target, vocab['PAD'])
            greedy = np.argmax(log_probs, axis=1)
            collapsed = [int(v) for i, v in enumerate(greedy) if (i == 0 or v != greedy[i - 1]) and v not in silence]
            error_rate = edit_distance(collapsed, target) / len(target)
            segments = []
            def boundary(frame):
                # 400-sample receptive field and 320-sample stride at 16 kHz.
                return 0.0 if frame == 0 else duration if frame == len(path) else min(duration, (320 * frame + 40) / 16000)
            for i, phone in enumerate(phones):
                frames = np.flatnonzero(path == 2 * i + 1)
                start, end = int(frames[0]), int(frames[-1]) + 1
                segments.append({**phone, 'start_frame': start, 'end_frame': end,
                                 'start_seconds': boundary(start), 'end_seconds': boundary(end),
                                 'mean_posterior': float(np.exp(log_probs[frames, target[i]]).mean())})
            flags = []
            if error_rate > .2:
                flags.append('phoneme_error_rate_above_0.2')
            if min(s['mean_posterior'] for s in segments) < .1:
                flags.append('low_posterior_phone')
            record.update({'aligned': True, 'token_ids': ids, 'phonemes': segments,
                           'greedy_phones': [inverse_vocab[i] for i in collapsed],
                           'greedy_phone_error_rate': error_rate, 'review_flags': flags,
                           'ctc_score': score, 'blank_penalty': args.blank_penalty,
                           'seconds': duration, 'analysis_seconds': time.monotonic() - started,
                           'wav_sha256': item['wav_sha256'], 'text_sha256': sha(text_path),
                           'log_probs_sha256': sha(destination / 'log_probs.npy')})
        except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            record.update({'aligned': False, 'error': str(error)})
        (destination / 'alignment.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        rows.append({k: v for k, v in record.items() if k not in ('token_ids', 'phonemes', 'greedy_phones')})
        print(key, 'aligned' if record['aligned'] else 'needs repair', record.get('review_flags', []), flush=True)
    if args.ids and set(args.ids) != {row['id'] for row in rows}:
        raise ValueError('Requested utterance not found')
    report = {'rows': rows, 'dataset_sha256': sha(dataset_path), 'model_sha256': sha(model),
              'g2p_executable_sha256': sha(args.executable), 'dictionary_sha256': sha(args.dictionary),
              'runtime': ort.__version__, 'numpy': np.__version__, 'training_approved': False,
              'limitations': ['CTC emission spans are not verified phoneme-duration targets.',
                              'Accent/control token durations are not assigned.',
                              'Manual or independent boundary checks remain required.']}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['data', 'model', 'executable', 'dictionary', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--blank-penalty', type=float, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.blank_penalty) or not 0 <= args.blank_penalty <= 10:
        parser.error('blank-penalty must be finite and within 0..10')
    run(args)
