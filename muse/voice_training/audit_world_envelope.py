#!/usr/bin/env python3
"""包絡補正の保存点・予測再現・音程等の不変性・比較WAVを照合する。"""
import argparse
import json
from pathlib import Path
import numpy as np
import torch
from train_world import load_data,digest,write_json
from world_context_model import load as load_base
from world_envelope_model import load
from world_envelope_loss import EnvelopeLoss
from train_world_envelope import prepare,evaluate,BASE_SHA
from world_vocoder import synthesize
from audit_world_training import pcm
from compare_band_results import save_previews


def means(rows):
    return {k:float(np.mean([r[k] for r in rows])) for k in rows[0] if k!='id'}


def run(args):
    torch.set_num_threads(2)
    manifest=json.loads((args.trusted/'SHA256.json').read_text())
    for name,sha in manifest.items():
        if digest(args.trusted/name)!=sha:raise ValueError('保存入力が変わっています: '+name)
    if digest(args.trusted/'base.pt')!=BASE_SHA:raise ValueError('開始モデルが異なります')
    base,base_state=load_base(args.trusted/'base.pt')
    loss=EnvelopeLoss();data=load_data(args.trusted/'data');prepare(data,base,loss,'cpu')
    initial,_=load(args.trusted/'base.pt',initial=True)
    analysis=args.result/'analysis';analysis.mkdir(exist_ok=False)
    report={'input_files_verified':len(manifest),'base':{},'conditions':{},'quality_approved':False,
            'inference_uses_teacher_WORLD_parameters':False,'inference_uses_teacher_codec_latent':True,
            'firmware_changed':False,'text_to_speech_ready':False,'C152_real_time_verified':False}
    for split in ('train','validation','test'):
        score,rows=evaluate(initial,data[split],loss)
        report['base'][split]={'score':score,'means':means(rows),'rows':rows}
    reply_item=next(x for x in data['test'] if x['id']=='mf07-test_reply')
    with torch.no_grad():f,p,_=base.physical(reply_item['z'][None])
    previous=synthesize(f[0].numpy(),p[0].numpy(),reply_item['samples'])
    np.testing.assert_array_equal(previous,np.load(args.previous/'analysis/context_mf07-test_reply.npy',allow_pickle=False))
    audios={'previous_model':previous}
    for condition in ('control','focused'):
        folder=args.result/'output'/condition
        info=json.loads((folder/'run.json').read_text());done=json.loads((folder/'completed.json').read_text())
        if info['condition']!=condition or info['base_sha256']!=BASE_SHA:raise ValueError('比較条件が異なります')
        if (info['seed'],info['steps_requested'],done['updates'],info['batch'],info['learning_rate'])!=(20261117,5000,5000,8,.0002):
            raise ValueError('学習条件または更新数が異なります')
        if info['split_ids']!={k:[i['id'] for i in v] for k,v in data.items()}:raise ValueError('分割が変わっています')
        for name,sha in info['code_sha256'].items():
            if digest(args.trusted/name)!=sha or digest(Path(__file__).with_name(name))!=sha:raise ValueError('コードが変わっています')
        model,state=load(folder/'best.pt');endpoint,end_state=load(folder/'endpoint.pt')
        if state['condition']!=condition or end_state['condition']!=condition:raise ValueError('保存形式の条件が異なります')
        for m in (model,endpoint):
            for k,v in m.base.state_dict().items():torch.testing.assert_close(v,base_state['model'][k],atol=0,rtol=0)
        if sum(p.numel() for p in model.parameters())!=info['parameters'] or sum(p.numel() for p in model.parameters() if p.requires_grad)!=info['trainable_parameters']:
            raise ValueError('モデル規模が異なります')
        logs=[json.loads(x) for x in (folder/'metrics.jsonl').read_text().splitlines()]
        if [r['step'] for r in logs]!=list(range(0,5001,100)):raise ValueError('学習記録が不足しています')
        score,step=min([(r['score'],r['step']) for r in logs]+[(done['endpoint_validation_score'],5000)])
        if state['step']!=step or done['best_step']!=step or end_state['step']!=5000:raise ValueError('保存点選択が異なります')
        np.testing.assert_allclose(state['score'],score,atol=1e-8,rtol=1e-8)
        np.testing.assert_allclose(done['initial_validation_score'],report['base']['validation']['score'],atol=1e-6,rtol=1e-6)
        endpoint_score,_=evaluate(endpoint,data['validation'],loss)
        np.testing.assert_allclose(endpoint_score,done['endpoint_validation_score'],atol=1e-5,rtol=1e-5)
        summary={'updates':done['updates'],'best_step':step,'checkpoint_sha256':digest(folder/'best.pt'),
                 'training_seconds':done['training_seconds'],'parameters':info['parameters'],'trainable_parameters':info['trainable_parameters'],'clips':[]}
        for split in ('train','validation','test'):
            score,rows=evaluate(model,data[split],loss)
            if split!='train':
                key='best_validation_score' if split=='validation' else 'test_score'
                np.testing.assert_allclose(score,done[key],atol=1e-5,rtol=1e-5)
                for actual,saved in zip(rows,done[split+'_rows'],strict=True):
                    if actual['id']!=saved['id']:raise ValueError('評価文章が異なります')
                    for k in actual:
                        if k!='id':np.testing.assert_allclose(actual[k],saved[k],atol=1e-5,rtol=1e-5)
            summary[split]={'score':score,'means':means(rows),'rows':rows}
        for item in data['test']:
            with torch.no_grad():
                full=model(item['z'][None]);cached=model.from_base(item['z'][None],item['base_prediction'][None])
                torch.testing.assert_close(full,cached,atol=0,rtol=0)
                torch.testing.assert_close(full[:,60:],item['base_prediction'][None,60:],atol=0,rtol=0)
                features,f0,uv=model.physical(item['z'][None]);bf,bp,bu=base.physical(item['z'][None])
                torch.testing.assert_close(features[:,60:],bf[:,60:],atol=0,rtol=0)
                torch.testing.assert_close(f0,bp,atol=0,rtol=0);torch.testing.assert_close(uv,bu,atol=0,rtol=0)
            arrays={k:v[0].numpy() for k,v in {'features':features,'f0':f0,'uv_probability':uv}.items()}
            with np.load(folder/'predictions'/(item['id']+'.npz'),allow_pickle=False) as saved:
                if set(saved.files)!=set(arrays):raise ValueError('予測項目が異なります')
                for k,v in arrays.items():np.testing.assert_array_equal(v,saved[k])
            y=synthesize(arrays['features'],arrays['f0'],item['samples'])
            if not np.isfinite(y).all() or not np.any(y):raise ValueError('波形が不正です')
            np.save(analysis/(condition+'_'+item['id']+'.npy'),y,allow_pickle=False)
            summary['clips'].append({'id':item['id'],'raw_peak':float(np.abs(y).max()),'non_envelope_parameters_unchanged':True})
            if item['id']=='mf07-test_reply':audios[condition]=y
        report['conditions'][condition]=summary
    audios['diagnostic_teacher_envelope']=pcm(args.diagnostic/'teacher_envelope.wav','70c050648291dc0609fecbdfd19fd1a5b00d208a24095a6329f6b9ff10f3cc31')
    if any(y.shape!=(282240,) for y in audios.values()):raise ValueError('試聴長が異なります')
    save_previews(analysis/'reply',audios,{'comparison':'same text and RMS; diagnostic uses teacher envelope'},pcm_scale=32768)
    stats={}
    for name in audios:
        p=analysis/'reply'/(name+'.wav');y=pcm(p,digest(p));integer=np.rint(y*32768).astype(np.int32)
        stats[name]={'sha256':digest(p),'samples':len(y),'rms':float(np.sqrt(np.mean(y.astype(float)**2))),
                     'peak':float(np.abs(y).max()),'rails':int(np.count_nonzero((integer==32767)|(integer==-32768)))}
        if len(y)!=282240 or stats[name]['rails']:raise ValueError('試聴WAVの範囲が不正です')
    if np.ptp([v['rms'] for v in stats.values()])>5e-7:raise ValueError('平均音量が異なります')
    report.update(integrity_passed=True,base_model_unchanged=True,non_envelope_parameters_unchanged=True,
                  previous_raw_reproduced=True,wav_validation=stats,
                  candidate_by_validation=min(report['conditions'],key=lambda c:report['conditions'][c]['validation']['score']))
    write_json(analysis/'audit.json',report)
    print(json.dumps({'candidate':report['candidate_by_validation'],'base':report['base']['test']['means'],
        'conditions':{c:{'step':v['best_step'],'test':v['test']['means']} for c,v in report['conditions'].items()},'wav_validation':stats},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('result','trusted','previous','diagnostic'):p.add_argument('--'+name,type=Path,required=True)
    run(p.parse_args())
