#!/usr/bin/env python3
"""照合済みの92文をWORLDの音響パラメータへ変換する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import pyworld as pw
from world_vocoder import SR, SP_DIM, AP_DIM, FRAME_MS, extract


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(args):
    trusted=args.trusted_input
    for name,sha in json.loads((trusted/'SHA256.json').read_text()).items():
        if digest(trusted/name)!=sha:raise ValueError('信頼済み入力が一致しません')
    audit=json.loads((args.result/'analysis/audit.json').read_text())
    if not audit['integrity_passed']:raise ValueError('全データの照合が必要です')
    sys.path.insert(0,str(trusted.resolve()))
    from expanded_data import load_expanded
    data=load_expanded(args.result/'data',trusted/'expanded_texts.json')
    args.output.mkdir(parents=True,exist_ok=False)
    rows=[];started=time.monotonic()
    for split,items in data.items():
        for item in items:
            sid=item['row']['sample_id']; y=item['y'][:item['samples']].numpy()
            features,log_f0,uv=extract(y)
            latent=np.load(args.result/'data'/item['row']['latent'],allow_pickle=False)[0]
            times=np.arange(features.shape[1])*FRAME_MS/1000
            positions=np.clip(times*25-.5,0,len(latent)-1)
            lo=positions.astype(int);hi=np.minimum(lo+1,len(latent)-1);a=(positions-lo)[:,None]
            z=((1-a)*latent[lo]+a*latent[hi]).T.astype(np.float32)
            out=args.output/(sid+'.npz')
            np.savez_compressed(out,z=z,features=features,log_f0=log_f0,uv=uv)
            valid=np.exp(log_f0[uv>0]); row={'id':sid,'split':split,'samples':len(y),'frames':features.shape[1],
                'source_wav_sha256':item['row']['wav_sha256'],'source_latent_sha256':item['row']['latent_sha256'],
                'file':out.name,'sha256':digest(out),'voiced_fraction':float(uv.mean()),
                'f0_range':[float(valid.min()),float(valid.max())] if len(valid) else None}
            rows.append(row)
            print('WORLD_FEATURES',len(rows),sid,round(float(uv.mean()),3),flush=True)
    report={'kind':'world_parameters_from_verified_mf07_teacher','pyworld':pw.__version__,'code_sha256':digest(__file__),
            'vocoder_code_sha256':digest(Path(__file__).with_name('world_vocoder.py')),
            'sr':SR,'frame_period_ms':FRAME_MS,'spectral_dimensions':SP_DIM,'aperiodic_dimensions':AP_DIM,
            'pitch_extraction':'DIO plus StoneMask, 65–600Hz; labels are estimates, not pitch ground truth',
            'latent_alignment':'native 25fps centers aligned to WORLD frame timestamps; endpoint repeat for synthesis length',
            'seconds':time.monotonic()-started,'rows':rows,'quality_approved':False,'firmware_changed':False}
    (args.output/'dataset.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('result',type=Path);p.add_argument('--trusted-input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
