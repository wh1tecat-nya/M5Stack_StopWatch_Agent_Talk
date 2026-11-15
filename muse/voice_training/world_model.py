"""codec潜在表現から声の高さ・音色・非周期成分を予測する試作。"""
import torch
from torch import nn
import torch.nn.functional as F

from world_vocoder import SR, SP_DIM, AP_DIM

CONTEXT, SEGMENT = 16, 96


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.depth = nn.Conv1d(64,64,7,padding=3,groups=64)
        self.expand = nn.Conv1d(64,128,1)
        self.reduce = nn.Conv1d(128,64,1)

    def forward(self,x):
        return x + .2*self.reduce(F.silu(self.expand(self.depth(x))))


class WorldPredictor(nn.Module):
    def __init__(self,z_mean,z_std,y_mean,y_std,f0_mean,f0_std):
        super().__init__()
        for k,v in {'z_mean':z_mean,'z_std':z_std,'y_mean':y_mean,
                    'y_std':y_std,'f0_mean':f0_mean,'f0_std':f0_std}.items():
            self.register_buffer(k,v.clone())
        self.input=nn.Conv1d(32,64,3,padding=1)
        self.blocks=nn.Sequential(*(Block() for _ in range(4)))
        self.output=nn.Conv1d(64,SP_DIM+AP_DIM+2,1)

    def forward(self,z):
        return self.output(self.blocks(F.silu(self.input((z-self.z_mean)/self.z_std))))

    def physical(self,z):
        result=self(z)
        coded=result[:,:SP_DIM+AP_DIM]*self.y_std+self.y_mean
        f0=torch.exp((result[:,-2:-1]*self.f0_std+self.f0_mean).clamp(4.174387,6.39693))
        uv=torch.sigmoid(result[:,-1:])
        return coded,f0*(uv>=.5),uv


def objective(prediction, features, log_f0, voiced, model):
    normalized=(features-model.y_mean)/model.y_std
    spectral=F.smooth_l1_loss(prediction[:,:SP_DIM+AP_DIM],normalized)
    f0_target=(log_f0-model.f0_mean)/model.f0_std
    pitch=(F.smooth_l1_loss(prediction[:,-2:-1],f0_target,reduction='none')*voiced).sum()/voiced.sum().clamp_min(1)
    voicing=F.binary_cross_entropy_with_logits(prediction[:,-1:],voiced)
    return spectral+.2*pitch+.2*voicing, {'spectral':spectral,'pitch':pitch,'voicing':voicing}
