#!/usr/bin/env python3
"""振幅予測の残差を調べる。正解を使う診断であり、推論時の補正ではない。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import torch
from magnitude_model import SCALE, spectrum, waveform, unit_phase


def check_pair(predicted, target):
    if predicted.ndim != 2 or predicted.shape != target.shape or min(target.shape) < 2:
        raise ValueError('周波数×時間の同じ形状が必要です')
    if any(not torch.isfinite(x).all() or (x < 0).any() for x in (predicted, target)):
        raise ValueError('非負の有限振幅が必要です')
    if target.square().sum() <= 0:
        raise ValueError('無音だけの正解では比率を評価できません')


def bounded_oracle(base, target):
    """現行補正の±5と最終log値0〜12だけを課した、正解を使う上限診断。"""
    check_pair(base, target)
    b, t = torch.log1p(base / SCALE), torch.log1p(target / SCALE)
    return SCALE * torch.expm1((b + (t - b).clamp(-5, 5)).clamp(0, 12))


def metrics(predicted, target):
    check_pair(predicted, target)
    power = target.square().sum(0)
    active = power > power.max() * 1e-4
    quiet = ~active
    weak = target < target.max(0, keepdim=True).values * .01
    region = active[None].expand_as(target)
    norm = torch.linalg.vector_norm(target)
    gain = (predicted * target).sum(0) / predicted.square().sum(0).clamp_min(1e-20)
    fitted = predicted * gain
    lp, lt = torch.log1p(predicted / SCALE), torch.log1p(target / SCALE)

    def fraction(x, mask):
        return float(x.square()[mask].sum() / x.square().sum().clamp_min(1e-20))

    def delta_error(dimension):
        a, b = torch.diff(lp, dim=dimension), torch.diff(lt, dim=dimension)
        return float(torch.linalg.vector_norm(a - b) / torch.linalg.vector_norm(b).clamp_min(1e-20))

    return {
        'relative_l2': float(torch.linalg.vector_norm(predicted-target) / norm),
        'relative_l2_after_best_per_frame_gain': float(torch.linalg.vector_norm(fitted-target) / norm),
        'frequency_log_difference_relative_error': delta_error(0),
        'temporal_log_difference_relative_error': delta_error(1),
        'active_frames': int(active.sum()), 'quiet_frames': int(quiet.sum()),
        'predicted_active_energy_in_target_weak_bins_fraction': float(
            predicted.square()[weak & region].sum() / predicted.square()[region].sum().clamp_min(1e-20)),
        'target_active_energy_in_same_bins_fraction': float(
            target.square()[weak & region].sum() / target.square()[region].sum()),
        'predicted_quiet_energy_fraction': fraction(predicted, quiet[None].expand_as(target)),
        'target_quiet_energy_fraction': fraction(target, quiet[None].expand_as(target)),
    }


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    audit = json.loads((args.result / 'analysis/audit.json').read_text())
    if not audit['integrity_passed']:
        raise ValueError('先に全データと保存モデルの照合が必要です')
    for name, expected in json.loads((args.trusted_input / 'SHA256.json').read_text()).items():
        if digest(args.trusted_input / name) != expected:
            raise ValueError('信頼済み入力が一致しません: ' + name)
    if digest(Path(__file__).with_name('magnitude_model.py')) != digest(args.trusted_input / 'magnitude_model.py'):
        raise ValueError('診断に使う振幅モデルの実装が変わっています')
    checkpoint = args.result / 'output/best.pt'
    if digest(checkpoint) != audit['best_checkpoint_sha256']:
        raise ValueError('照合済みの重みが変わっています')
    sys.path.insert(0, str(args.trusted_input.resolve()))
    import train_magnitude as training
    from expanded_data import load_expanded
    torch.set_num_threads(2)
    model = training.load_model(args.trusted_input / 'base.pt', 'cpu').eval()
    model.correction.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True)['correction'])
    data = load_expanded(args.result / 'data', args.trusted_input / 'expanded_texts.json')
    report = {
        'kind': 'teacher_assisted_error_diagnosis_not_inference_or_quality_score',
        'checkpoint_sha256': digest(checkpoint), 'code_sha256': digest(__file__),
        'quality_approved': False, 'firmware_changed': False,
        'masks': 'active: target frame power > peak frame power * 1e-4; weak: target bin amplitude < frame peak * .01',
        'caveats': ['弱い周波数の成分は必ずしも可聴ノイズではありません。',
                    '時間差分・周波数差分の誤差は聴感のブレやにじみを直接測るものではありません。',
                    'iSTFT後は振幅と位相が共に変わり得ます。',
                    '比較文章は今後の設計に使用されるため、独立した最終評価ではありません。'],
        'samples': {},
    }
    with torch.no_grad():
        for split in ('validation', 'test'):
            for item in data[split]:
                z, samples = item['z'][None], item['samples']
                s = model.base_spectrum(z, samples)
                base = s.abs()[0]
                target = spectrum(item['y'][None, :samples]).abs()[0]
                predicted = model.correction(z, s.abs())[0]
                oracle = bounded_oracle(base, target)
                # 再構成後も同じ基準で比べる。全文RMSだけを揃え、時間変化は保持する。
                phase = unit_phase(s)[0]
                output = waveform(predicted * phase, samples)
                target3 = waveform(target * phase, samples)
                output *= target3.square().mean().sqrt() / output.square().mean().sqrt().clamp_min(1e-20)
                report['samples'][item['row']['sample_id']] = {
                    'split': split, 'additional': item['row']['sample_id'].startswith('mf07-expand_'),
                    'predicted_magnitude_vs_teacher': metrics(predicted, target),
                    'range_limited_teacher_vs_teacher': metrics(oracle, target),
                    'resynthesized_equal_rms_vs_diagnostic_3': metrics(spectrum(output).abs(), spectrum(target3).abs()),
                }
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'samples': len(report['samples']), 'output': str(args.output), 'diagnostic_only': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--trusted-input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
