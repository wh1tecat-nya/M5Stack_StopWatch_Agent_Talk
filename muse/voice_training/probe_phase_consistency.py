#!/usr/bin/env python3
"""保存モデルの位相を再推定して比較する。教師使用の診断と推論候補を分ける。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import torch
from magnitude_model import spectrum, waveform
from phase_consistency import refine_phase
from phase_probe import load
from compare_band_results import save_previews

TARGET_SHA = '5bcfe7d28d56dce7f014ab0f822864f3ef67e1024ceff282b1ee031b92452b89'


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(args):
    audit = json.loads((args.result/'analysis/audit.json').read_text())
    if not audit['integrity_passed'] or digest(args.result/'output/best.pt') != audit['best_checkpoint_sha256']:
        raise ValueError('保存結果の照合が必要です')
    for name, sha in json.loads((args.trusted_input/'SHA256.json').read_text()).items():
        if digest(args.trusted_input/name) != sha: raise ValueError('入力が変わっています: '+name)
    if digest(Path(__file__).with_name('magnitude_model.py')) != digest(args.trusted_input/'magnitude_model.py'):
        raise ValueError('モデルの実装が一致しません')
    if digest(args.target) != TARGET_SHA: raise ValueError('基準Bが一致しません')
    sys.path.insert(0,str(args.trusted_input.resolve()))
    import train_magnitude as training
    from expanded_data import load_expanded
    torch.set_num_threads(2)
    model = training.load_model(args.trusted_input/'base.pt','cpu').eval()
    model.correction.load_state_dict(torch.load(args.result/'output/best.pt',map_location='cpu',weights_only=True)['correction'])
    data = load_expanded(args.result/'data',args.trusted_input/'expanded_texts.json')
    args.output.mkdir(parents=True,exist_ok=False)
    b = load(args.target)
    br = waveform(spectrum(b),len(b))
    report = {'kind':'phase_consistency_experiment', 'target':'accepted Irodori MF07 at 22050 Hz (B)',
              'target_sha256':TARGET_SHA, 'checkpoint_sha256':audit['best_checkpoint_sha256'],
              'code_sha256':digest(__file__), 'refinement_code_sha256':digest(Path(__file__).with_name('phase_consistency.py')),
              'iterations':32, 'momentum':0, 'new_training':False, 'quality_approved':False,
              'device_real_time_verified':False, 'text_to_speech_ready':False,
              'model_candidate_uses_teacher_at_inference':False,
              'B_roundtrip_relative_l2':float(torch.linalg.vector_norm(b-br)/torch.linalg.vector_norm(b)),
              'implementation':'Whole utterance on Mac CPU, two threads; no streaming or MCU implementation.',
              'reference':'https://librosa.org/doc/0.11.0/generated/librosa.griffinlim.html', 'samples':[]}
    with torch.no_grad():
        for item in data['test']:
            sid=item['row']['sample_id']; z=item['z'][None]; n=item['samples']
            base = model.base_spectrum(z,n)[0]
            mag = model.correction(z,base.abs()[None])[0]
            started=time.perf_counter()
            refined, history = refine_phase(mag,base,n,32)
            elapsed=time.perf_counter()-started
            original=model(z,n)[0]
            teacher=item['y'][:n]
            target_mag=spectrum(teacher).abs()
            entry={'id':sid, 'seconds':n/22050, 'refine_seconds_cpu':elapsed,
                   'history':history, 'teacher_magnitude_error':{}}
            for name,y in [('original',original),('refined32',refined)]:
                entry['teacher_magnitude_error'][name]=float(torch.linalg.vector_norm(spectrum(y).abs()-target_mag)/torch.linalg.vector_norm(target_mag))
            # この枝だけは教師振幅を借りる。推論候補や採用モデルと混同しない。
            if sid=='mf07-test_reply':
                oracle, oh = refine_phase(target_mag,base,n,32)
                entry['teacher_assisted_diagnostic_history']=oh
                save_previews(args.output/'reply',{'model_previous':original.numpy(),
                    'model_phase_refined32':refined.numpy(),'accepted_reference_B':b.numpy()},
                    {'magnitude':4900,'phase_refinement':32}, pcm_scale=32768)
                save_previews(args.output/'diagnostic_only',{'teacher_magnitude_refined32':oracle.numpy(),
                    'accepted_reference_B':b.numpy()},{'phase_refinement':32}, pcm_scale=32768)
            report['samples'].append(entry)
            print(json.dumps({'id':sid,'phase_refine_seconds':round(elapsed,4),
                              'magnitude_error_before':history[0]['magnitude_relative_l2'],
                              'magnitude_error_after':history[-1]['magnitude_relative_l2']}),flush=True)
    with (args.output/'report.json').open('x') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('result',type=Path)
    p.add_argument('--trusted-input',type=Path,required=True)
    p.add_argument('--target',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
