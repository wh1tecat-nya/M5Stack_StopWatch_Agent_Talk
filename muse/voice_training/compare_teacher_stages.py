#!/usr/bin/env python3
"""元のIrodori出力、周波数変換のみ、診断③を音量を揃えて比較する。"""
import argparse
import hashlib
import json
from pathlib import Path
import wave

import numpy as np
from scipy.signal import resample_poly

TEACHER_SHA = '14477312d3543f1b52b77b73f73b9fdd3ea5c00415b7965ef096ad0857de578c'
DIAGNOSTIC_SHA = 'fb2eead93b998c1e3f5b15838091fa102e203dd7a8bb6d1e5f4d0e597247ef33'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_pcm(path, rate, expected_sha):
    if digest(path) != expected_sha:
        raise ValueError('比較対象の音声が一致しません: ' + str(path))
    with wave.open(str(path)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (rate, 1, 2):
            raise ValueError('音声形式が異なります')
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768


def prepare(teacher, diagnostic, output):
    a = read_pcm(teacher, 48000, TEACHER_SHA)
    b = resample_poly(a, 147, 320).astype(np.float32)
    c = read_pcm(diagnostic, 22050, DIAGNOSTIC_SHA)
    if len(a) != 614400 or len(b) != 282240 or len(c) != 282240:
        raise ValueError('同じ12.8秒の文章ではありません')
    audios = {'A_irodori_48k': (48000, a),
              'B_irodori_22k': (22050, b),
              'C_diagnostic_3': (22050, c)}
    rmss = {k: float(np.sqrt(np.mean(y.astype(np.float64)**2))) for k, (_, y) in audios.items()}
    target_rms = min(.1, *[.9 * rmss[k] / float(np.max(np.abs(y))) for k, (_, y) in audios.items()])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        'kind': 'quality_target_reassessment', 'quality_approved': False,
        'source_sha256': {'teacher': TEACHER_SHA, 'diagnostic_3': DIAGNOSTIC_SHA},
        'code_sha256': digest(__file__), 'normalization': 'equal full-utterance RMS; not LUFS',
        'target_rms': target_rms, 'seconds': 12.8,
        'teacher': 'MF07 / 4 steps / seed 102 / Python FP32; official generation output including its standard watermark',
        'processing': {
            'A_irodori_48k': '元の48kHz生成出力に一定ゲインのみ。',
            'B_irodori_22k': 'Aの音量調整前データを学習時と同じresample_poly(147,320)で22,050Hzへ変換し、一定ゲイン。',
            'C_diagnostic_3': '以前に試聴した教師振幅＋小型モデル位相の診断③に一定ゲインのみ。'
        },
        'caveats': ['新たなモデルや改善音声ではありません。',
                    'A/Bにも違和感がある場合、Irodori生成・参照音声・標準処理・再生経路を追加確認します。',
                    'Cだけで増える場合も、位相単独の原因確定ではなく再構成全体の差です。'],
        'files': {},
    }
    stored_rms = []
    for name, (rate, audio) in audios.items():
        gain = target_rms / rmss[name]
        scaled = audio.astype(np.float64) * gain
        if not np.isfinite(scaled).all() or np.max(np.abs(scaled)) > .900001:
            raise ValueError('振幅または有限値の検査に失敗しました')
        pcm = np.rint(scaled * 32768).astype('<i2')
        path = output / (name + '.wav')
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        saved = read_pcm(path, rate, digest(path)).astype(np.float64)
        error = float(np.max(np.abs(saved - scaled)))
        if error > .5 / 32768 + 1e-10 or np.any(np.abs(pcm.astype(np.int32)) >= 32767):
            raise ValueError('PCM量子化または上限到達の検査に失敗しました')
        rms = float(np.sqrt(np.mean(saved**2)))
        stored_rms.append(rms)
        report['files'][name] = {'sample_rate': rate, 'frames': len(pcm), 'gain': gain,
            'rms': rms, 'peak': float(np.max(np.abs(saved))), 'pcm_rails': 0,
            'quantization_max_error': error, 'sha256': digest(path)}
    if max(stored_rms) - min(stored_rms) > 2e-6:
        raise ValueError('保存音声の平均RMSが揃っていません')
    (output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--teacher', type=Path, required=True)
    parser.add_argument('--diagnostic', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.teacher, args.diagnostic, args.output)
