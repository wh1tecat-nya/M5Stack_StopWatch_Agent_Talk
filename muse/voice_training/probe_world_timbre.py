#!/usr/bin/env python3
"""直前の文脈拡張候補を保ち、包絡と非周期成分を分けて聴取する。"""
import argparse
import json
from pathlib import Path

import numpy as np
import torch

from train_world import load_data, digest, write_json
from world_context_model import load
from world_timbre_diagnosis import components
from world_vocoder import synthesize
from audit_world_training import pcm
from compare_band_results import save_previews

CHECKPOINT_SHA='6d1af2387f9a7173bef480fe06a682c15bbace62938905e7fbf990f27b791d0d'
APPROVED_SHA='3bd909da60c80be8a36b4ea13e6784c7574cc893088e62e9aaaf7bfde8b63889'


def run(args):
    torch.set_num_threads(2)
    folder=args.context_result/'output/context'
    if digest(folder/'best.pt')!=CHECKPOINT_SHA:
        raise ValueError('直前に聴取したモデルではありません')
    audit=json.loads((args.context_result/'analysis/audit.json').read_text())
    if not audit['integrity_passed'] or audit['conditions']['context']['checkpoint_sha256']!=CHECKPOINT_SHA:
        raise ValueError('照合済みの保存点ではありません')
    info=json.loads((folder/'run.json').read_text())
    for name,sha in info['code_sha256'].items():
        if digest(Path(__file__).with_name(name))!=sha:
            raise ValueError('推論コードが変わっています')
    item=next(i for i in load_data(args.data)['test'] if i['id']=='mf07-test_reply')
    model,_=load(folder/'best.pt')
    with torch.no_grad():
        features,f0,_=model.physical(item['z'][None])
    pf,pp=features[0].numpy(),f0[0,0].numpy()
    teacher=item['features'].numpy()
    with np.load(args.reference/'analysis_parameters.npz',allow_pickle=False) as r:
        np.testing.assert_array_equal(teacher,r['features'])
        teacher_f0=r['f0'].copy()
    pairs=components(teacher,pf,pp)
    args.output.mkdir(parents=True,exist_ok=False)
    audios={k:synthesize(f,p,item['samples']) for k,(f,p) in pairs.items()}
    np.testing.assert_array_equal(audios['model_all'],np.load(args.context_result/'analysis/context_mf07-test_reply.npy',allow_pickle=False))
    approved=pcm(args.reference/'world_compact_parameters.wav',APPROVED_SHA)
    teacher_all=synthesize(teacher,teacher_f0,item['samples'])
    gain=.1/float(np.sqrt(np.mean(teacher_all**2)))
    np.testing.assert_array_equal(np.rint(teacher_all*gain*32768).astype('<i2'),np.rint(approved*32768).astype('<i2'))
    audios['approved_world_compact']=approved
    for name,y in audios.items():
        if y.shape!=(282240,) or not np.isfinite(y).all():
            raise ValueError('合成波形が不正です')
        np.save(args.output/(name+'.npy'),y,allow_pickle=False)
    save_previews(args.output/'listening',audios,{'checkpoint_sha256':CHECKPOINT_SHA},pcm_scale=32768)
    stats={}
    for name in audios:
        path=args.output/'listening'/(name+'.wav')
        y=pcm(path,digest(path)); integer=np.rint(y*32768).astype(np.int32)
        stats[name]={'sha256':digest(path),'samples':len(y),'rms':float(np.sqrt(np.mean(y.astype(float)**2))),
                     'peak':float(np.abs(y).max()),'rails':int(np.count_nonzero((integer==32767)|(integer==-32768)))}
        if len(y)!=282240 or stats[name]['rails']:
            raise ValueError('試聴音声の長さまたはPCM範囲が不正です')
    if np.ptp([r['rms'] for r in stats.values()])>5e-7:
        raise ValueError('平均RMSが一致しません')
    report={'checkpoint_sha256':CHECKPOINT_SHA,'code_sha256':{n:digest(Path(__file__).with_name(n)) for n in ('probe_world_timbre.py','world_timbre_diagnosis.py')},
            'model_prediction_reproduced':True,'approved_resynthesis_reproduced':True,
            'conditions':{'teacher_envelope':{'spectral_envelope':'teacher','aperiodicity':'predicted','f0_and_voicing':'predicted'},
                          'teacher_aperiodicity':{'spectral_envelope':'predicted','aperiodicity':'teacher','f0_and_voicing':'predicted'}},
            'wav_validation':stats,'diagnostic_only':True,'quality_approved':False,'firmware_changed':False,
            'limitations':['教師成分を使う診断であり、学習モデルの改善版ではない。','F0と有声判定は2本で同一。非周期成分の復号条件は別で、声の周期性へ影響する。','成分間の依存があるため、単独の原因を断定しない。','全文RMS一致でありLUFS一致ではない。']}
    write_json(args.output/'report.json',report)
    print(json.dumps({'prediction_reproduced':True,'approved_reproduced':True,'wav_validation':stats},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('context-result','data','reference','output'):p.add_argument('--'+name,type=Path,required=True)
    run(p.parse_args())
