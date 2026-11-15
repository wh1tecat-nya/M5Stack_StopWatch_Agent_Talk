"""初期出力を保持して幅を広げるWORLD予測器の容量比較。"""
import torch
from torch import nn
import torch.nn.functional as F

from train_world import STAT_NAMES
from world_model import WorldPredictor

WIDTHS = {'control': 64, 'capacity': 96}
HALO = 16


class Block(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.depth = nn.Conv1d(width, width, 7, padding=3, groups=width)
        self.expand = nn.Conv1d(width, width * 2, 1)
        self.reduce = nn.Conv1d(width * 2, width, 1)

    def forward(self, x):
        return x + .2 * self.reduce(F.silu(self.expand(self.depth(x))))


class CapacityPredictor(WorldPredictor):
    def __init__(self, *statistics, width):
        if width not in WIDTHS.values():
            raise ValueError('未検証のモデル幅です')
        super().__init__(*statistics)
        self.input = nn.Conv1d(32, width, 3, padding=1)
        self.blocks = nn.Sequential(*(Block(width) for _ in range(4)))
        self.output = nn.Conv1d(width, 64, 1)


@torch.no_grad()
def initialize(base_state, condition):
    width = WIDTHS[condition]
    source = base_state['model']
    model = CapacityPredictor(*(source[k] for k in STAT_NAMES), width=width)
    if width == 64:
        model.load_state_dict(source, strict=True)
        return model.eval()
    # 複製した特徴への重みを複製数で割り、初期の合計出力を維持する。
    generator = torch.Generator().manual_seed(619)
    channels = torch.cat((torch.arange(64), torch.randperm(64, generator=generator)[:width-64]))
    hidden = torch.cat((torch.arange(128), torch.randperm(128, generator=generator)[:2*width-128]))
    channel_counts = torch.bincount(channels, minlength=64)[channels].float()
    hidden_counts = torch.bincount(hidden, minlength=128)[hidden].float()
    target = model.state_dict()
    target['input.weight'].copy_(source['input.weight'][channels])
    target['input.bias'].copy_(source['input.bias'][channels])
    for index in range(4):
        prefix = f'blocks.{index}.'
        target[prefix+'depth.weight'].copy_(source[prefix+'depth.weight'][channels])
        target[prefix+'depth.bias'].copy_(source[prefix+'depth.bias'][channels])
        target[prefix+'expand.weight'].copy_(source[prefix+'expand.weight'][hidden][:,channels] / channel_counts[None,:,None])
        target[prefix+'expand.bias'].copy_(source[prefix+'expand.bias'][hidden])
        target[prefix+'reduce.weight'].copy_(source[prefix+'reduce.weight'][channels][:,hidden] / hidden_counts[None,:,None])
        target[prefix+'reduce.bias'].copy_(source[prefix+'reduce.bias'][channels])
    target['output.weight'].copy_(source['output.weight'][:,channels] / channel_counts[None,:,None])
    target['output.bias'].copy_(source['output.bias'])
    # 同じ出力の複製が永遠に同じ更新になることを避ける。和が0の差だけを加える。
    for extra in range(64, width):
        original = int(channels[extra])
        noise = torch.randn(64, 1, generator=generator) * .001
        target['output.weight'][:,original].add_(noise)
        target['output.weight'][:,extra].sub_(noise)
    model.load_state_dict(target, strict=True)
    return model.eval()


def load(path, *, initial_condition=None, device='cpu'):
    state = torch.load(path, map_location='cpu', weights_only=True)
    if initial_condition is not None:
        if 'format' in state:
            raise ValueError('開始モデルの形式が異なります')
        model = initialize(state, initial_condition)
    else:
        if state.get('format') != 'world_capacity_v1' or state['width'] != WIDTHS[state['condition']]:
            raise ValueError('容量比較用のモデルではありません')
        model = CapacityPredictor(*(state['model'][k] for k in STAT_NAMES), width=state['width'])
        model.load_state_dict(state['model'], strict=True)
    return model.to(device).eval(), state


def save(path, model, condition, step, score):
    temporary = path.with_suffix('.temporary')
    torch.save({'format': 'world_capacity_v1', 'condition': condition, 'width': WIDTHS[condition],
                'model': {k:v.detach().cpu() for k,v in model.state_dict().items()},
                'step':step, 'score':score}, temporary)
    temporary.replace(path)
