"""包絡補正版を、量子化しないFP32配列と構造情報へ書き出す。"""
import hashlib
import json
from pathlib import Path
import numpy as np
import torch

from world_model import WorldPredictor
from world_context_model import configure
from world_envelope_model import EnvelopeRefiner

FORMAT='sumi_world_envelope_fp32_v1'
ARCHITECTURE={'base_width':64,'base_dilations':[1,2,4,8],
              'refiner_width':48,'refiner_blocks':2,'input_channels':32,
              'spectral_channels':60,'aperiodicity_channels':2,
              'frame_period_ms':5.0,'sample_rate':22050}


def template():
    base=WorldPredictor(torch.zeros(1,32,1),torch.ones(1,32,1),
                        torch.zeros(1,62,1),torch.ones(1,62,1),torch.tensor(5.),torch.tensor(.3))
    return EnvelopeRefiner(configure(base,'context')).eval()


def export(model,folder,source_sha):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    parameter_names=set(dict(model.named_parameters()))
    rows=[];chunks=[];offset=0
    for name,value in model.state_dict().items():
        array=value.detach().cpu().numpy().astype('<f4',copy=False)
        if not np.isfinite(array).all():raise ValueError('非有限の重みです')
        blob=array.tobytes(order='C')
        rows.append({'name':name,'shape':list(array.shape),'offset_bytes':offset,'bytes':len(blob),
                     'kind':'parameter' if name in parameter_names else 'normalization'})
        chunks.append(blob);offset+=len(blob)
    blob=b''.join(chunks)
    (folder/'weights.f32').write_bytes(blob)
    metadata={'format':FORMAT,'architecture':ARCHITECTURE,'dtype':'float32','byte_order':'little',
              'source_checkpoint_sha256':source_sha,'weights_sha256':hashlib.sha256(blob).hexdigest(),
              'bytes':len(blob),'parameters':sum(p.numel() for p in model.parameters()),
              'normalization_values':sum(b.numel() for b in model.buffers()),'tensors':rows,
              'quantized':False,'firmware_ready':False,
              'requires':'native neural inference, WORLD synthesis, text-to-latent frontend, device validation'}
    (folder/'model.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    return metadata


def restore(folder):
    folder=Path(folder)
    metadata=json.loads((folder/'model.json').read_text())
    model=template();expected=model.state_dict()
    if (metadata['format']!=FORMAT or metadata['architecture']!=ARCHITECTURE
            or metadata['dtype']!='float32' or metadata['byte_order']!='little'
            or metadata['parameters']!=114636 or metadata['normalization_values']!=190):
        raise ValueError('未対応のモデル構造です')
    blob=(folder/'weights.f32').read_bytes()
    if len(blob)!=459304 or len(blob)!=metadata['bytes'] or hashlib.sha256(blob).hexdigest()!=metadata['weights_sha256']:
        raise ValueError('重みファイルが一致しません')
    tensors={};offset=0
    for row in metadata['tensors']:
        name=row['name']
        if name not in expected or name in tensors or row['shape']!=list(expected[name].shape):
            raise ValueError('重み名または形状が不正です')
        count=expected[name].numel();size=count*4
        if row['offset_bytes']!=offset or row['bytes']!=size or offset+size>len(blob):
            raise ValueError('重み範囲が不正です')
        array=np.frombuffer(blob,dtype='<f4',count=count,offset=offset).copy().reshape(row['shape'])
        if not np.isfinite(array).all():raise ValueError('非有限の重みです')
        tensors[name]=torch.from_numpy(array);offset+=size
    if set(tensors)!=set(expected) or offset!=len(blob):raise ValueError('重みが不足しています')
    for name in ('base.z_std','base.y_std','base.f0_std'):
        if not torch.all(tensors[name]>0):raise ValueError('正規化の分母が不正です')
    model.load_state_dict(tensors,strict=True)
    return model.eval(),metadata
