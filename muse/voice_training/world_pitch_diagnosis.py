"""音程・有声判定の数値診断と、教師依存の成分入れ替え。"""
import numpy as np

from world_vocoder import SP_DIM, AP_DIM, F0_FLOOR

CONDITIONS = {
    'model_all': ('predicted', 'predicted'),
    'teacher_pitch_model_timbre': ('predicted', 'teacher'),
    'model_pitch_teacher_timbre': ('teacher', 'predicted'),
    'teacher_all': ('teacher', 'teacher'),
}


def component_pairs(teacher_features, teacher_f0, predicted_features, predicted_f0):
    """音程側にはF0と有声・無声を含み、音色側には包絡と非周期成分を含む。"""
    features = {'teacher': np.asarray(teacher_features), 'predicted': np.asarray(predicted_features)}
    pitches = {'teacher': np.asarray(teacher_f0), 'predicted': np.asarray(predicted_f0)}
    frames = pitches['teacher'].size
    if frames < 2:
        raise ValueError('2フレーム以上が必要です')
    for key in features:
        if (pitches[key].ndim != 1 or pitches[key].shape != (frames,)
                or features[key].shape != (SP_DIM + AP_DIM, frames)
                or not np.isfinite(features[key]).all() or not np.isfinite(pitches[key]).all()
                or np.any(pitches[key] < 0)):
            raise ValueError('成分の形状・時間軸・有限値が不正です')
    return {name: (features[color].copy(), pitches[pitch].copy())
            for name, (color, pitch) in CONDITIONS.items()}


def runs(mask):
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 1 or not len(mask):
        raise ValueError('空でない一次元マスクが必要です')
    boundaries = np.flatnonzero(np.r_[True, mask[1:] != mask[:-1], True])
    return [(bool(mask[a]), int(b - a)) for a, b in zip(boundaries[:-1], boundaries[1:])]


def pitch_metrics(reference_f0, predicted_f0):
    r, p = (np.asarray(x, dtype=np.float64) for x in (reference_f0, predicted_f0))
    if (r.ndim != 1 or r.shape != p.shape or len(r) < 2
            or not np.isfinite(r).all() or not np.isfinite(p).all() or np.any(r < 0) or np.any(p < 0)):
        raise ValueError('F0入力が不正です')
    rv, pv = r > 0, p > 0
    both = rv & pv
    consecutive = both[1:] & both[:-1]
    rc = 1200 * np.log2(np.maximum(r, F0_FLOOR))
    pc = 1200 * np.log2(np.maximum(p, F0_FLOOR))
    error = pc - rc

    def mean(values):
        return float(np.mean(values)) if len(values) else None

    rr, pr = runs(rv), runs(pv)
    return {'joint_voiced_frames': int(both.sum()),
            'pitch_mae_cents_joint_voiced': mean(abs(error[both])),
            'pitch_bias_cents_joint_voiced': mean(error[both]),
            'pitch_error_over_200c_fraction_joint_voiced': mean(abs(error[both]) > 200),
            'predicted_step_mae_cents': mean(abs(np.diff(pc)[consecutive])),
            'reference_step_mae_cents': mean(abs(np.diff(rc)[consecutive])),
            'step_error_mae_cents': mean(abs(np.diff(error)[consecutive])),
            'predicted_steps_over_100c': int((abs(np.diff(pc)[consecutive]) > 100).sum()),
            'uv_error_fraction': float(np.mean(rv != pv)),
            'predicted_uv_transitions': len(pr) - 1, 'reference_uv_transitions': len(rr) - 1,
            'predicted_runs_under_20ms': sum(n < 4 for _, n in pr),
            'reference_runs_under_20ms': sum(n < 4 for _, n in rr)}
