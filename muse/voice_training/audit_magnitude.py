#!/usr/bin/env python3
"""Colabの振幅学習を信頼済み入力と照合し、CPU再生成・試聴WAVを保存する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

import numpy as np
import torch

from compare_band_results import save_previews
from diagnose_decoder import compare


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def audit(result, trusted, output, expanded=False):
    checks = json.loads((trusted / 'SHA256.json').read_text())
    for name, expected in checks.items():
        if digest(trusted / name) != expected or digest(result / 'input' / name) != expected:
            raise ValueError('入力が一致しません: ' + name)
    sys.path.insert(0, str(trusted.resolve()))
    import train_magnitude as training
    from magnitude_model import magnitude_loss, spectrum, waveform, unit_phase
    torch.set_num_threads(2)
    model = training.load_model(trusted / 'base.pt', 'cpu').eval()
    ckpt = torch.load(result / 'output/best.pt', map_location='cpu', weights_only=True)
    complete = json.loads((result / 'output/completed.json').read_text())
    metrics = [json.loads(line) for line in (result / 'output/metrics.jsonl').read_text().splitlines()]
    best = min(metrics, key=lambda r: r['score'])
    assert best['step'] == ckpt['step'] == complete['best_step']
    assert abs(best['score'] - complete['best_validation_loss']) < 1e-7
    model.correction.load_state_dict(ckpt['correction'], strict=True)
    assert torch.equal(model.base.mean, model.correction.mean)
    assert torch.equal(model.base.std, model.correction.std)
    if expanded:
        from expanded_data import load_expanded
        old_rows = json.loads((trusted / 'data/dataset.json').read_text())
        extra_rows = json.loads((result / 'teacher/dataset.json').read_text())
        assert json.loads((result / 'data/dataset.json').read_text()) == old_rows + extra_rows
        plan = json.loads((result / 'teacher/plan.json').read_text())
        assert plan['selected_profiles'] == ['mf07']
        assert plan['manifest'] == json.loads((trusted / 'voice_manifest.json').read_text())
        data = load_expanded(result / 'data', trusted / 'expanded_texts.json')
        for row in old_rows:
            for key in ('wav', 'latent'):
                assert digest(result / 'data' / row[key]) == row[key + '_sha256']
        for row in extra_rows:
            for key in ('wav', 'latent'):
                assert digest(result / 'data' / row[key]) == digest(result / 'teacher' / row[key]) == row[key + '_sha256']
    else:
        data = training.load_data(trusted / 'data')
    validation, _ = training.evaluate(model.correction, training.prepare(data['validation'], model, 'cpu'), 'cpu')
    assert abs(validation - complete['best_validation_loss']) < 1e-5
    output.mkdir(parents=True, exist_ok=False)
    report = {'integrity_passed': False, 'input_files_verified': len(checks),
              'best_checkpoint_sha256': digest(result / 'output/best.pt'),
              'validation_cpu': validation, 'completed': complete,
              'quality_verified': False, 'real_time_on_C152_verified': False,
              'expanded_teacher_data': expanded,
              'samples': []}
    with torch.no_grad():
        for item in data['test']:
            name = item['row']['sample_id']
            z, samples = item['z'][None], item['samples']
            s = model.base_spectrum(z, samples)
            reference = item['y'][None, :samples]
            candidate = waveform(s, samples)
            predicted = model(z, samples)
            oracle = waveform(spectrum(reference).abs() * unit_phase(s), samples)
            signals = {'candidate': candidate, 'predicted': predicted,
                       'oracle_magnitude': oracle, 'reference': reference}
            differences = {}
            for key, value in signals.items():
                expected = np.load(result / 'output' / name / (key + '.npy'), allow_pickle=False)
                actual = value[0].numpy()
                assert actual.shape == expected.shape == (samples,)
                assert np.isfinite(expected).all() and np.isfinite(actual).all()
                differences[key] = float(np.max(np.abs(expected - actual)))
                np.testing.assert_allclose(expected, actual, atol=3e-5, rtol=1e-4)
            baseline_loss = float(magnitude_loss(s.abs(), spectrum(reference).abs()))
            predicted_loss = float(magnitude_loss(model.correction(z, s.abs()), spectrum(reference).abs()))
            audios = {k: v[0].numpy() for k, v in signals.items()}
            save_previews(output / name, audios, {'base': 2000, 'correction': ckpt['step']})
            for path in (output / name).glob('*.wav'):
                with wave.open(str(path)) as wav:
                    assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (22050, 1, 2, samples)
                    pcm = np.frombuffer(wav.readframes(samples), dtype='<i2').astype(float) / 32768
                    assert np.max(np.abs(pcm)) <= .9001
            report['samples'].append({'id': name, 'max_abs_cpu_gpu': differences,
                'baseline_magnitude_loss': baseline_loss, 'predicted_magnitude_loss': predicted_loss,
                'predicted_diagnostics': compare(audios['reference'], audios['predicted']),
                'baseline_diagnostics': compare(audios['reference'], audios['candidate'])})
    report['integrity_passed'] = True
    with (output / 'audit.json').open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'integrity_passed': True, 'validation': validation,
        'samples': [{k: r[k] for k in ('id', 'max_abs_cpu_gpu', 'baseline_magnitude_loss', 'predicted_magnitude_loss')} for r in report['samples']]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--trusted-input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expanded', action='store_true')
    args = parser.parse_args()
    audit(args.result, args.trusted_input, args.output, args.expanded)
