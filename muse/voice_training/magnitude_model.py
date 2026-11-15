"""固定した候補波形の位相を使い、振幅だけを学習する再構成モデル。"""
import torch
from torch import nn
import torch.nn.functional as F

SR, FFT, HOP, SCALE = 22050, 1024, 256, .01
CONTEXT, SEGMENT = 14, 64


def spectrum(y):
    return torch.stft(y, FFT, HOP, window=torch.hann_window(FFT, device=y.device,
                      dtype=y.dtype), center=True, return_complex=True)


def waveform(s, samples):
    return torch.istft(s, FFT, HOP, window=torch.hann_window(FFT, device=s.device,
                       dtype=s.real.dtype), center=True, length=samples)


def unit_phase(s):
    a = s.abs()
    return torch.where(a > 1e-10, s / a.clamp_min(1e-10), torch.ones_like(s))


def match_frames(z, frames):
    # center=TrueのSTFTは長さがhopの倍数なら末尾に1フレーム増える。
    if frames not in (z.shape[-1], z.shape[-1] + 1):
        raise ValueError('潜在表現とSTFTのフレーム数が一致しません')
    return F.pad(z, (0, frames - z.shape[-1]), mode='replicate')


class Residual(nn.Module):
    def __init__(self):
        super().__init__()
        self.dw = nn.Conv1d(64, 64, 7, padding=3, groups=64)
        self.up = nn.Conv1d(64, 128, 1)
        self.down = nn.Conv1d(128, 64, 1)

    def forward(self, x):
        return x + .2 * self.down(F.silu(self.up(self.dw(x))))


class MagnitudeCorrection(nn.Module):
    def __init__(self, mean, std):
        super().__init__()
        self.register_buffer('mean', mean.clone())
        self.register_buffer('std', std.clone())
        self.input = nn.Conv1d(32 + 513, 64, 1)
        self.blocks = nn.Sequential(*(Residual() for _ in range(4)))
        self.output = nn.Conv1d(64, 513, 1)
        nn.init.zeros_(self.output.weight)
        nn.init.zeros_(self.output.bias)

    def forward(self, z, base_magnitude):
        z = match_frames(z, base_magnitude.shape[-1])
        base = torch.log1p(base_magnitude / SCALE)
        x = torch.cat(((z - self.mean) / self.std, base), dim=1)
        correction = 5 * torch.tanh(self.output(self.blocks(F.silu(self.input(x)))) / 5)
        return SCALE * torch.expm1((base + correction).clamp(0, 12))


class FrozenPhaseReconstruction(nn.Module):
    """推論入力は潜在表現と出力長だけ。教師音声・教師振幅は受け取らない。"""
    def __init__(self, base, correction):
        super().__init__()
        self.base = base.eval().requires_grad_(False)
        self.correction = correction

    def train(self, mode=True):
        super().train(mode)
        self.base.eval()
        return self

    @torch.no_grad()
    def base_spectrum(self, z, samples):
        if not FFT <= samples <= z.shape[-1] * HOP:
            raise ValueError('出力長が不正です')
        return spectrum(self.base(z)[:, :samples])

    def forward(self, z, samples):
        s = self.base_spectrum(z, samples)
        return waveform(self.correction(z, s.abs()) * unit_phase(s), samples)


def magnitude_loss(predicted, target):
    log_error = (torch.log1p(predicted / SCALE) - torch.log1p(target / SCALE)).abs().mean()
    relative = (torch.linalg.vector_norm(predicted - target, dim=(-2, -1)) /
                torch.linalg.vector_norm(target, dim=(-2, -1)).clamp_min(1e-6)).mean()
    return log_error + .5 * relative


def batch_segments(items, rng, batch_size, device):
    result = [[], [], []]
    width = SEGMENT + 2 * CONTEXT
    for _ in range(batch_size):
        item = rng.choice(items)
        start = rng.randrange(item['z'].shape[-1] - width + 1)
        for rows, key in zip(result, ('z', 'base', 'target')):
            rows.append(item[key][:, start:start + width])
    return tuple(torch.stack(rows).to(device) for rows in result)


def convolution_macs_per_frame(model):
    return sum(m.out_channels * (m.in_channels // m.groups) * m.kernel_size[0]
               for m in model.modules() if isinstance(m, nn.Conv1d))
