#!/usr/bin/env python3
"""固定した予測器の包絡だけを補正し、従来型と形状重視の損失を比較する。"""
import argparse
import json
from pathlib import Path
import random
import time
import numpy as np
import torch

from train_world import load_data, export_predictions, digest, write_json
from world_context_model import load as load_base
from world_envelope_model import load, save, TRAIN_HALO
from world_envelope_loss import EnvelopeLoss

BASE_SHA='6d1af2387f9a7173bef480fe06a682c15bbace62938905e7fbf990f27b791d0d'
CODE_FILES=('train_world_envelope.py','world_envelope_model.py','world_envelope_loss.py','world_acoustic_loss.py','world_context_model.py','world_model.py','train_world.py','world_vocoder.py')
FIELDS=('z','features','log_f0','uv','frame_weight','base_prediction')


@torch.no_grad()
def prepare(data,base,loss,device):
    for items in data.values():
        for item in items:
            for k in FIELDS[:4]:item[k]=item[k].to(device)
            item['base_prediction']=base(item['z'][None])[0]
            item['frame_weight']=loss.frame_weights(item['features'][None],item['uv'][None])[0]


def batch_segments(items,rng):
    pieces={k:[] for k in FIELDS};width=96+2*TRAIN_HALO
    for _ in range(8):
        item=rng.choice(items);start=rng.randrange(item['z'].shape[1]-width+1)
        for k in FIELDS:pieces[k].append(item[k][:,start:start+width])
    return {k:torch.stack(v) for k,v in pieces.items()}


@torch.no_grad()
def evaluate(model,items,loss):
    model.eval();rows=[]
    for item in items:
        p=model.from_base(item['z'][None],item['base_prediction'][None])
        score,_=loss(p,item['features'][None],model,item['frame_weight'][None],'focused')
        rows.append({'id':item['id'],'score':float(score),**loss.diagnostics(p,item['features'][None],model,item['frame_weight'][None])})
    return float(np.mean([r['score'] for r in rows])),rows


def train_condition(args,condition,data,loss):
    torch.manual_seed(args.seed);rng=random.Random(args.seed)
    model,base_state=load(args.base,initial=True,device=args.device)
    folder=args.output/condition;folder.mkdir(parents=True,exist_ok=False)
    parameters=[p for p in model.parameters() if p.requires_grad]
    optimizer=torch.optim.AdamW(parameters,lr=.0002,weight_decay=.0001)
    initial,initial_rows=evaluate(model,data['validation'],loss)
    best,best_step,updates=initial,0,0
    save(folder/'best.pt',model,condition,0,initial)
    write_json(folder/'run.json',{'condition':condition,'base_sha256':BASE_SHA,'base_step':base_state['step'],
        'seed':args.seed,'steps_requested':args.steps,'max_seconds':args.max_seconds,'learning_rate':.0002,'batch':8,
        'parameters':sum(p.numel() for p in model.parameters()),'trainable_parameters':sum(p.numel() for p in parameters),
        'segment_frames':96,'cached_training_halo_frames':TRAIN_HALO,'inference_receptive_frames':99,
        'selection':'same focused envelope validation score for both conditions',
        'frozen':'complete base model, F0, voicing, aperiodicity, normalization',
        'cache':'base predictions from codec latent only; frame weights from full teacher utterance for training/evaluation only',
        'split_ids':{k:[x['id'] for x in v] for k,v in data.items()},
        'code_sha256':{n:digest(Path(__file__).with_name(n)) for n in CODE_FILES},
        'torch':str(torch.__version__),'numpy':str(np.__version__),'device':args.device,
        'quality_approved':False,'firmware_changed':False,'text_to_speech_ready':False})
    started=time.monotonic()
    with (folder/'metrics.jsonl').open('x') as log:
        log.write(json.dumps({'step':0,'score':initial,'validation':initial_rows})+'\n')
        while updates<args.steps and time.monotonic()-started<args.max_seconds:
            model.train();batch=batch_segments(data['train'],rng)
            p=model.from_base(batch['z'],batch['base_prediction'])[:,:,TRAIN_HALO:-TRAIN_HALO]
            target=batch['features'][:,:,TRAIN_HALO:-TRAIN_HALO]
            weight=batch['frame_weight'][:,:,TRAIN_HALO:-TRAIN_HALO]
            value,_=loss(p,target,model,weight,condition)
            if not torch.isfinite(value):raise ValueError('学習損失が非有限です')
            optimizer.zero_grad(set_to_none=True);value.backward()
            torch.nn.utils.clip_grad_norm_(parameters,5.,error_if_nonfinite=True)
            optimizer.step();updates+=1
            if updates%100==0 or updates==args.steps:
                score,rows=evaluate(model,data['validation'],loss)
                if score<best:
                    best,best_step=score,updates;save(folder/'best.pt',model,condition,updates,score)
                log.write(json.dumps({'step':updates,'score':score,'train_loss':float(value.detach()),'validation':rows})+'\n');log.flush()
                print('ENVELOPE_PROGRESS',condition,updates,round(score,6),flush=True)
        endpoint,_=evaluate(model,data['validation'],loss)
        if endpoint<best:
            best,best_step=endpoint,updates;save(folder/'best.pt',model,condition,updates,endpoint)
        save(folder/'endpoint.pt',model,condition,updates,endpoint)
    seconds=time.monotonic()-started
    model,_=load(folder/'best.pt',device=args.device)
    for k,v in model.base.state_dict().items():
        if not torch.equal(v.cpu(),base_state['model'][k].cpu()):raise ValueError('固定した予測器が変化しました')
    validation,validation_rows=evaluate(model,data['validation'],loss)
    test,test_rows=evaluate(model,data['test'],loss)
    export_predictions(model,data['test'],folder)
    write_json(folder/'completed.json',{'updates':updates,'best_step':best_step,'initial_validation_score':initial,
        'best_validation_score':validation,'endpoint_validation_score':endpoint,'test_score':test,
        'validation_rows':validation_rows,'test_rows':test_rows,'training_seconds':seconds,
        'base_model_unchanged':True,'quality_approved':False,'firmware_changed':False})
    print('ENVELOPE_COMPLETE',condition,updates,best_step,validation,flush=True)


def run(args):
    if digest(args.base)!=BASE_SHA:raise ValueError('開始モデルが異なります')
    torch.set_num_threads(2)
    torch.backends.cudnn.benchmark=False
    torch.backends.cudnn.allow_tf32=False
    torch.backends.cuda.matmul.allow_tf32=False
    data=load_data(args.data);base,_=load_base(args.base,device=args.device)
    loss=EnvelopeLoss().to(args.device);prepare(data,base,loss,args.device)
    args.output.mkdir(parents=True,exist_ok=False)
    for condition in ('control','focused'):train_condition(args,condition,data,loss)
    write_json(args.output/'completed.json',{'conditions':['control','focused'],'quality_approved':False})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('data','base','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--device',choices=['cpu','cuda'],default='cpu')
    p.add_argument('--steps',type=int,default=5000)
    p.add_argument('--max-seconds',type=int,default=600)
    p.add_argument('--seed',type=int,default=20261117)
    args=p.parse_args()
    if not 1<=args.steps<=5000 or not 1<=args.max_seconds<=600:p.error('試験の上限を超えています')
    run(args)
