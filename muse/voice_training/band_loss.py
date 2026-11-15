"""小型decoderの比較試験用。各帯域の相対誤差を同じ重みで扱う。"""
import torch


def balanced_spectral_loss(generated, target):
    """静かな帯域の無制限な増幅を防ぐ床付きMR-STFT損失。

    1024点で子音の時間変化、4096点で細かな周波数差を比較する。
    位相を直接教師へ固定する損失ではない。
    """
    terms = []
    for size, hop in [(1024, 256), (4096, 512)]:
        window = torch.hann_window(size, device=target.device, dtype=target.dtype)
        def magnitude(y):
            return torch.stft(y, size, hop, window=window, return_complex=True,
                              normalized=True).abs()
        fake, real = magnitude(generated), magnitude(target)
        frequencies = torch.fft.rfftfreq(size, 1 / 22050, device=target.device)
        for low, high in [(0, 1000), (1000, 3000), (3000, 6000), (6000, 11026)]:
            mask = (frequencies >= low) & (frequencies < high)
            f, r = fake[:, mask], real[:, mask]
            scale = r.square().mean((1, 2), keepdim=True).sqrt().clamp_min(.001)
            log_difference = (torch.log1p(f / scale) - torch.log1p(r / scale)).abs().mean()
            convergence = ((f-r).square().mean((1, 2)).clamp_min(1e-12).sqrt() /
                           scale.flatten()).mean()
            terms.append(log_difference + .5 * convergence)
    return torch.stack(terms).mean()
