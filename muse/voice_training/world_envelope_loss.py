"""包絡の音量と形を分け、聞こえる区間の形状・変化を学習する。"""
import math
import torch
import torch.nn.functional as F
from world_acoustic_loss import AcousticLoss


class EnvelopeLoss(AcousticLoss):
    @torch.no_grad()
    def frame_weights(self,features,voiced):
        # 切り出す前の一文全体から求め、学習と評価で同じ重みを使う。
        db=self.spectrum_db(features[:,:60])
        level=torch.logsumexp(db*(math.log(10)/10),dim=-1)*(10/math.log(10))-10*math.log10(db.shape[-1])
        threshold=torch.quantile(level,.95,dim=1,keepdim=True)-35
        weight=.1+.9*torch.sigmoid((level-threshold)/4)
        return torch.maximum(weight,voiced[:,0])[:,None]

    def terms(self,prediction,features,model,weight):
        physical=prediction[:,:60]*model.y_std[:,:60]+model.y_mean[:,:60]
        error=self.spectrum_db(physical)-self.spectrum_db(features[:,:60])
        level=(error*self.weights).sum(-1,keepdim=True)
        shape=error-level
        frame=weight[:,0]
        def mean(values,fw=self.weights,tw=frame):
            return ((values*fw).sum(-1)*tw).sum()/tw.sum().clamp_min(1e-8)
        def robust(x):return F.smooth_l1_loss(x,torch.zeros_like(x),reduction='none')
        envelope=mean(robust(error/6))
        formant=mean(robust(shape/3))
        # 約129Hz離れた周波数点の差を合わせ、単純な平滑化にはしない。
        fw=self.weights[6:];fw=fw/fw.sum()
        slope=mean(robust((shape[:,:,6:]-shape[:,:,:-6])/2),fw=fw)
        trajectory=[]
        for lag in (4,8):
            pair=torch.minimum(frame[:,lag:],frame[:,:-lag])
            trajectory.append(mean(robust((shape[:,lag:]-shape[:,:-lag])/3),tw=pair))
        trajectory=sum(trajectory)/len(trajectory)
        coefficients=F.smooth_l1_loss(prediction[:,:60],(features[:,:60]-model.y_mean[:,:60])/model.y_std[:,:60])
        original=self.band_mean(robust(error/6))+.2*self.band_mean(robust((error[:,1:]-error[:,:-1])/3))+.05*coefficients
        focused=envelope+.5*formant+.2*slope+.2*trajectory+.02*coefficients
        return {'control':original,'focused':focused,'envelope':envelope,'shape':formant,'slope':slope,'trajectory':trajectory}

    def forward(self,prediction,features,model,weight,condition='focused'):
        terms=self.terms(prediction,features,model,weight)
        return terms[condition],terms

    @torch.no_grad()
    def diagnostics(self,prediction,features,model,weight):
        physical=prediction[:,:60]*model.y_std[:,:60]+model.y_mean[:,:60]
        error=self.spectrum_db(physical)-self.spectrum_db(features[:,:60])
        level=(error*self.weights).sum(-1,keepdim=True)
        shape=error-level
        frame=weight[:,0]
        def avg(x,tw=frame):return float(((x*self.weights).sum(-1)*tw).sum()/tw.sum().clamp_min(1e-8))
        return {'envelope_mae_db':float(self.band_mean(error.abs())),
                'weighted_envelope_mae_db':avg(error.abs()),
                'weighted_shape_mae_db':avg(shape.abs()),
                'weighted_level_mae_db':float((level[:,:,0].abs()*frame).sum()/frame.sum()),
                'weighted_shape_step20ms_error_db':avg((shape[:,4:]-shape[:,:-4]).abs(),torch.minimum(frame[:,4:],frame[:,:-4]))}
