"""音程を固定し、スペクトル包絡と非周期成分を個別に交換する診断。"""
import numpy as np
from world_vocoder import SP_DIM, AP_DIM


def components(teacher_features, predicted_features, predicted_f0):
    teacher, predicted, f0 = (np.asarray(x) for x in (teacher_features, predicted_features, predicted_f0))
    if (f0.ndim != 1 or len(f0) < 2 or teacher.shape != (SP_DIM + AP_DIM, len(f0))
            or predicted.shape != teacher.shape or not all(np.isfinite(x).all() for x in (teacher,predicted,f0))
            or np.any(f0 < 0)):
        raise ValueError('成分の形状・時間軸・有限値が不正です')
    envelope, aperiodicity = predicted.copy(), predicted.copy()
    envelope[:SP_DIM] = teacher[:SP_DIM]
    aperiodicity[SP_DIM:] = teacher[SP_DIM:]
    return {name: (values.copy(), f0.copy()) for name, values in
            {'model_all':predicted, 'teacher_envelope':envelope,
             'teacher_aperiodicity':aperiodicity, 'teacher_timbre':teacher}.items()}
