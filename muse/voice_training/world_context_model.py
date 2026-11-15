"""重み数を変えずに、WORLD予測器が参照する前後の範囲を比較する。"""
import torch

from train_world import STAT_NAMES
from world_model import WorldPredictor

DILATIONS = {'control': (1, 1, 1, 1), 'context': (1, 2, 4, 8)}
HALO = 48


def configure(model, condition):
    if condition not in DILATIONS:
        raise ValueError('不明な比較条件です')
    for block, dilation in zip(model.blocks, DILATIONS[condition], strict=True):
        block.depth.dilation = (dilation,)
        block.depth.padding = (3 * dilation,)
    return model


def load(path, *, initial_condition=None, device='cpu'):
    state = torch.load(path, map_location='cpu', weights_only=True)
    if initial_condition is None:
        if state.get('format') != 'world_context_v1':
            raise ValueError('文脈比較用の保存モデルではありません')
        condition = state['condition']
    else:
        if 'format' in state:
            raise ValueError('追加学習の開始モデル形式が異なります')
        condition = initial_condition
    model = WorldPredictor(*(state['model'][k] for k in STAT_NAMES))
    model.load_state_dict(state['model'], strict=True)
    return configure(model, condition).to(device).eval(), state


def save(path, model, condition, step, score):
    temporary = path.with_suffix('.temporary')
    torch.save({'format': 'world_context_v1', 'condition': condition,
                'model': {k: v.detach().cpu() for k, v in model.state_dict().items()},
                'step': step, 'score': score}, temporary)
    temporary.replace(path)
