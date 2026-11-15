#!/usr/bin/env python3
"""聴感改善を確認した保存点を保持し、移植用の書き出しと別文章を検証する。"""
import argparse
import json
from pathlib import Path
import shutil
import wave

import numpy as np
import torch
from train_world import load_data,digest,write_json
from world_envelope_model import load,INFERENCE_HALO
from world_envelope_export import export,restore
from world_vocoder import synthesize,SR
from audit_world_training import pcm
from compare_band_results import save_previews

CHECKPOINT_SHA='1fd7d966f410f7e29aee6e1ee2f8ad64bfd87cbd8847057be93931d2384616d1'
SELECTION=(('sibilants','mf07-expand_test_04'),('numbers','mf07-expand_test_03'),('long','mf07-expand_test_08'))


def waveform_stats(path):
    y=pcm(path,digest(path));integer=np.rint(y*32768).astype(np.int32)
    return {'sha256':digest(path),'samples':len(y),'seconds':len(y)/SR,
            'rms':float(np.sqrt(np.mean(y.astype(float)**2))),'peak':float(np.abs(y).max()),
            'rails':int(np.count_nonzero((integer==32767)|(integer==-32768)))}


@torch.no_grad()
def run(args):
    torch.set_num_threads(2)
    source=args.result/'output/focused/best.pt'
    if digest(source)!=CHECKPOINT_SHA:raise ValueError('ユーザーが聴取した保存点ではありません')
    audit=json.loads((args.result/'analysis/audit.json').read_text())
    if not audit['integrity_passed'] or audit['conditions']['focused']['checkpoint_sha256']!=CHECKPOINT_SHA:
        raise ValueError('モデルの照合が必要です')
    info=json.loads((args.result/'output/focused/run.json').read_text())
    for name,sha in info['code_sha256'].items():
        if digest(Path(__file__).with_name(name))!=sha:raise ValueError('モデルのコードが変更されています')
    model,_=load(source)
    data=load_data(args.data)
    args.output.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(source,args.output/'baseline.pt')
    if digest(args.output/'baseline.pt')!=CHECKPOINT_SHA:raise ValueError('保存点のコピーが一致しません')
    metadata=export(model,args.output/'fp32',CHECKPOINT_SHA)
    restored,_=restore(args.output/'fp32')
    for name,value in model.state_dict().items():torch.testing.assert_close(value,restored.state_dict()[name],atol=0,rtol=0)
    rows=[];audios={};selection=dict((sid,label) for label,sid in SELECTION)
    texts={'mf07-'+r['id']:r for f in ('pilot_texts.json','expanded_texts.json') for r in json.loads(Path(__file__).with_name(f).read_text())}
    for item in data['test']:
        z=item['z'][None];original=model(z);full=restored(z)
        torch.testing.assert_close(full,original,atol=0,rtol=0)
        chunks=[]
        for start in range(0,z.shape[-1],64):
            stop=min(start+64,z.shape[-1]);left=max(0,start-INFERENCE_HALO);right=min(z.shape[-1],stop+INFERENCE_HALO)
            chunk=restored(z[:,:,left:right])[:,:,start-left:stop-left]
            chunks.append(chunk)
        chunked=torch.cat(chunks,dim=-1)
        torch.testing.assert_close(chunked,full,atol=2e-5,rtol=2e-5)
        torch.testing.assert_close(chunked[:,-1:]>=0,full[:,-1:]>=0,atol=0,rtol=0)
        features,f0,uv=restored.physical(z)
        y=synthesize(features[0].numpy(),f0[0].numpy(),item['samples'])
        np.testing.assert_array_equal(y,np.load(args.result/'analysis'/('focused_'+item['id']+'.npy'),allow_pickle=False))
        rows.append({'id':item['id'],'frames':z.shape[-1],'full_prediction_max_abs_difference':float((full-original).abs().max()),
                     'chunked_prediction_max_abs_difference':float((chunked-full).abs().max()),'voicing_labels_identical':True,
                     'full_waveform_exactly_reproduced':True})
        if item['id'] in selection:
            if item['split']!='test' or texts[item['id']]['split']!='test':raise ValueError('学習に使った文章が混ざっています')
            label=selection[item['id']]
            audios[label+'_model']=y
            reference_f0=np.exp(item['log_f0'][0].numpy())*item['uv'][0].numpy()
            audios[label+'_teacher_world']=synthesize(item['features'].numpy(),reference_f0,item['samples'])
    if len(audios)!=6:raise ValueError('比較文章が不足しています')
    save_previews(args.output/'listening',audios,{'checkpoint_sha256':CHECKPOINT_SHA},pcm_scale=32768)
    stats={name:waveform_stats(args.output/'listening'/(name+'.wav')) for name in audios}
    if any(r['rails'] for r in stats.values()) or np.ptp([r['rms'] for r in stats.values()])>5e-7:
        raise ValueError('試聴音声の平均音量またはPCM範囲が不正です')
    silence=np.zeros(round(.5*SR),dtype='<i2')
    segments=[];position=0
    for label,sid in SELECTION:
        n=stats[label+'_model']['samples']
        if n!=stats[label+'_teacher_world']['samples']:raise ValueError('比較音声の長さが異なります')
        segments.append({'id':sid,'label':label,'text':texts[sid]['text'],'start_seconds':position/SR,'end_seconds':(position+n)/SR})
        position+=n+len(silence)
    montage_stats={}
    for kind in ('model','teacher_world'):
        parts=[]
        for index,(label,_) in enumerate(SELECTION):
            with wave.open(str(args.output/'listening'/(label+'_'+kind+'.wav'))) as w:
                parts.append(np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').copy())
            if index<2:parts.append(silence)
        path=args.output/'listening'/('three_sentences_'+kind+'.wav')
        with wave.open(str(path),'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(SR);w.writeframes(np.concatenate(parts).tobytes())
        montage_stats[kind]=waveform_stats(path)
    if montage_stats['model']['samples']!=montage_stats['teacher_world']['samples'] or abs(montage_stats['model']['rms']-montage_stats['teacher_world']['rms'])>5e-7:
        raise ValueError('連結した比較音声が一致していません')
    conv_macs=sum(m.out_channels*m.in_channels//m.groups*m.kernel_size[0] for m in model.modules() if isinstance(m,torch.nn.Conv1d))*200
    report={'checkpoint_sha256':CHECKPOINT_SHA,'export':metadata,'portable_weights_roundtrip_exact':True,
            'test_rows':rows,'samples':segments,'wav_validation':stats,'montage_validation':montage_stats,
            'conv_macs_per_audio_second':conv_macs,'inference_halo_frames_used':INFERENCE_HALO,'output_chunk_frames':64,
            'new_training_performed':False,'weights_changed':False,'quantized':False,'firmware_changed':False,
            'quality_scope':'先の返答文で大幅改善。別文章と実機での品質は未承認。',
            'text_to_speech_ready':False,'C152_real_time_verified':False,
            'limitations':['別の3文は学習には未使用だが、比較データとして以前から設計に使用済み。独立した新規文章の最終評価ではない。',
                           '書き出した重みをPythonで復元して照合した検証。C/C++推論やESP32実測ではない。',
                           'MAC数は畳み込みのみ。WORLD合成、活性化、メモリー転送、文章処理、分割の重複計算を含まない。',
                           '入力は教師codec潜在表現であり、任意文章からのTTSは未完成。']}
    write_json(args.output/'baseline.json',report)
    print(json.dumps({'bytes':metadata['bytes'],'parameters':metadata['parameters'],'conv_macs_per_audio_second':conv_macs,
                      'max_chunk_difference':max(r['chunked_prediction_max_abs_difference'] for r in rows),'samples':segments,
                      'montage':montage_stats},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('result','data','output'):p.add_argument('--'+name,type=Path,required=True)
    run(p.parse_args())
