"""既存の音程・非周期成分を固定し、スペクトル包絡だけを補正する。"""
import torch
from torch import nn
import torch.nn.functional as F

from world_model import WorldPredictor
from world_context_model import load as load_context, configure
from train_world import STAT_NAMES

TRAIN_HALO = 4
INFERENCE_HALO = 50


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.depth = nn.Conv1d(48,48,3,padding=1,groups=48)
        self.expand = nn.Conv1d(48,96,1)
        self.reduce = nn.Conv1d(96,48,1)

    def forward(self,x):
        return x + .2*self.reduce(F.silu(self.expand(self.depth(x))))


class EnvelopeRefiner(nn.Module):
    def __init__(self,base):
        super().__init__()
        self.base=base.requires_grad_(False).eval()
        self.input=nn.Conv1d(96,48,3,padding=1)
        self.blocks=nn.Sequential(Block(),Block())
        self.output=nn.Conv1d(48,60,1)
        nn.init.zeros_(self.output.weight)
        nn.init.zeros_(self.output.bias)

    @property
    def z_mean(self): return self.base.z_mean
    @property
    def z_std(self): return self.base.z_std
    @property
    def y_mean(self): return self.base.y_mean
    @property
    def y_std(self): return self.base.y_std
    @property
    def f0_mean(self): return self.base.f0_mean
    @property
    def f0_std(self): return self.base.f0_std

    def train(self,mode=True):
        super().train(mode)
        self.base.eval()
        return self

    def from_base(self,z,prediction):
        inputs=torch.cat(((z-self.z_mean)/self.z_std,prediction),dim=1)
        correction=self.output(self.blocks(F.silu(self.input(inputs))))
        return torch.cat((prediction[:,:60]+correction,prediction[:,60:]),dim=1)

    def forward(self,z):
        with torch.no_grad(): prediction=self.base(z)
        return self.from_base(z,prediction)

    def physical(self,z):
        result=self(z)
        coded=result[:,:62]*self.y_std+self.y_mean
        f0=torch.exp((result[:,-2:-1]*self.f0_std+self.f0_mean).clamp(4.174387,6.39693))
        uv=torch.sigmoid(result[:,-1:])
        return coded,f0*(uv>=.5),uv


def load(path,*,initial=False,device='cpu'):
    if initial:
        base,state=load_context(path)
        if state['condition']!='context':raise ValueError('診断に使った文脈モデルが必要です')
        model=EnvelopeRefiner(base)
    else:
        state=torch.load(path,map_location='cpu',weights_only=True)
        if state.get('format')!='world_envelope_v1':raise ValueError('包絡補正用モデルではありません')
        base_state={k.removeprefix('base.'):v for k,v in state['model'].items() if k.startswith('base.')}
        base=configure(WorldPredictor(*(base_state[k] for k in STAT_NAMES)),'context')
        base.load_state_dict(base_state,strict=True)
        model=EnvelopeRefiner(base)
        model.load_state_dict(state['model'],strict=True)
    return model.to(device).eval(),state


def save(path,model,condition,step,score):
    temporary=path.with_suffix('.temporary')
    torch.save({'format':'world_envelope_v1','condition':condition,
                'model':{k:v.detach().cpu() for k,v in model.state_dict().items()},
                'step':step,'score':score},temporary)
    temporary.replace(path)
