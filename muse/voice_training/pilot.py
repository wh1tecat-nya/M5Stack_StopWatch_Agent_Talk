#!/usr/bin/env python3
"""固定した3方式で教師音声を生成する。学習や本体への書き込みは行わない。"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def implementation_code(function):
    """計測用フックをデコレーターのラッパーではなく実装本体へ付ける。"""
    return inspect.unwrap(function).__code__


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save_json(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def pcm_storage_gain(peak):
    """PCM保存時だけ余裕を設ける。学習では記録したゲインを戻す。"""
    if not math.isfinite(peak) or not 0 <= peak <= 4:
        raise ValueError('保存する音声のピークが不正です')
    return min(1., .98 / max(peak, 1e-8))


def read_reference(path, manifest):
    spec = manifest['reference']
    blob = Path(path).read_bytes()
    if hashlib.sha256(blob).hexdigest() != spec['file_sha256']:
        raise ValueError('参照音声パッケージのSHA-256が一致しません')
    if blob[:8] != b'SUMIRF01':
        raise ValueError('参照形式が異なります')
    metadata_length, payload_length = struct.unpack_from('<II', blob, 8)
    start = (16 + metadata_length + 3) & ~3
    metadata = json.loads(blob[16:16 + metadata_length])
    payload = blob[start:]
    if len(payload) != payload_length or payload_length != 1125 * 32 * 4:
        raise ValueError('参照サイズが異なります')
    if hashlib.sha256(payload).hexdigest() != spec['payload_sha256']:
        raise ValueError('参照データが異なります')
    if metadata['shape'] != spec['shape'] or metadata['clipCount'] != len(spec['clip_frames']):
        raise ValueError('参照の形状が異なります')
    offset = 0
    for clip, count in zip(metadata['clips'], spec['clip_frames']):
        chunk = payload[offset * 128:(offset + count) * 128]
        if (clip['frameOffset'] != offset or clip['frames'] != count
                or clip['sha256'] != hashlib.sha256(chunk).hexdigest()):
            raise ValueError('参照クリップの境界が異なります')
        offset += count
    if offset != 1125 or not all(math.isfinite(x[0]) for x in struct.iter_unpack('<f', payload)):
        raise ValueError('不正な参照値です')
    return metadata, payload


def read_plan(text_path=None):
    manifest = json.loads((HERE / 'voice_manifest.json').read_text())
    texts = json.loads((text_path or HERE / 'pilot_texts.json').read_text())
    ids, seen_text = set(), set()
    for row in texts:
        if row['id'] in ids or row['text'] in seen_text:
            raise ValueError('重複した文章です。同じ文章を評価へ漏らさないでください')
        if row['split'] not in {'train', 'validation', 'test'}:
            raise ValueError('未知のデータ区分です')
        if not row['id'].replace('_', '').isalnum() or not row['text'].strip():
            raise ValueError('不正な文章IDまたは空の文章です')
        ids.add(row['id'])
        seen_text.add(row['text'])
    if {p['id'] for p in manifest['profiles']} != {'anime40', 'mf07', 'mf08'}:
        raise ValueError('A/B/Cすべての方式が必要です')
    return manifest, texts


def setup(work, profile=None):
    manifest, _ = read_plan()
    work.mkdir(parents=True, exist_ok=True)
    repo = work / 'Irodori-TTS'
    source = manifest['source']

    def run(args, cwd=None):
        subprocess.run(args, cwd=cwd, check=True, timeout=1200)

    if repo.exists():
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
        if sha != source['revision']:
            raise ValueError('既存のソース版が異なります')
    else:
        run(['git', 'clone', '--no-checkout', source['repository'], str(repo)])
        run(['git', 'checkout', '--detach', source['revision']], cwd=repo)
    if digest(repo / 'uv.lock') != source['lock_sha256']:
        raise ValueError('依存関係の固定ファイルが異なります')
    run([sys.executable, '-m', 'pip', 'install', 'uv==' + source['uv']])
    run([sys.executable, '-m', 'uv', 'sync', '--python', source['python'], '--frozen', '--extra', 'cu128'], cwd=repo)
    python = repo / '.venv/bin/python'
    command = [str(python), str(HERE / 'pilot.py'), 'cache', '--work', str(work)]
    if profile:
        command.extend(['--profile', profile])
    run(command, cwd=work)
    print('SETUP_COMPLETE', flush=True)


def selected_profiles(manifest, profile):
    profiles = [p for p in manifest['profiles'] if profile is None or p['id'] == profile]
    if not profiles:
        raise ValueError('指定した教師モデルがありません')
    return profiles


def cache(work, profile=None):
    from huggingface_hub import snapshot_download, hf_hub_download
    manifest, _ = read_plan()
    models = {}
    for profile in selected_profiles(manifest, profile):
        key = profile['repository']
        if key not in models:
            models[key] = snapshot_download(key, revision=profile['revision'],
                                           allow_patterns=['model.safetensors', 'tokenizer/*'])
    codec = manifest['codec']
    codec_path = hf_hub_download(codec['repository'], filename=codec['filename'], revision=codec['revision'])
    watermark = manifest['watermark']
    snapshot_download(watermark['repository'], revision=watermark['revision'],
                      allow_patterns=['44_1_khz/73999_iteration/*'], local_dir=str(work / 'Models'))
    files = []
    for repo_id, directory in models.items():
        for path in sorted(Path(directory).rglob('*')):
            if path.is_file():
                files.append({'repository': repo_id, 'name': str(path.relative_to(directory)),
                              'bytes': path.stat().st_size, 'sha256': digest(path)})
    files.append({'name': 'codec/weights.pth', 'bytes': Path(codec_path).stat().st_size, 'sha256': digest(codec_path)})
    save_json(work / 'cache.json', {'models': models, 'codec': codec_path, 'files': files})
    print('CACHE_COMPLETE', len(files), flush=True)


def generate(work, reference, output, profile=None, text_path=None):
    manifest, texts = read_plan(text_path)
    metadata, payload = read_reference(reference, manifest)
    repo = work / 'Irodori-TTS'
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    if sha != manifest['source']['revision']:
        raise ValueError('生成ソースの版が異なります')
    if output.exists():
        raise FileExistsError('既存結果は上書きしません。失敗した場合も先にログを確認してください')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(repo))
    # SilentCipherの固定版は ../Models を検索する。
    os.chdir(repo)
    import numpy as np
    import torch
    from irodori_tts.inference_runtime import InferenceRuntime, RuntimeKey, SamplingRequest, save_wav
    import importlib.metadata
    if importlib.metadata.version('torch') != manifest['source']['torch']:
        raise ValueError('Torchの版が異なります')
    if not torch.cuda.is_available():
        raise RuntimeError('GPUがありません')
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    output.mkdir(parents=True)
    save_json(output / 'plan.json', {'manifest': manifest, 'texts': texts, 'reference': metadata,
                                   'selected_profiles': [p['id'] for p in selected_profiles(manifest, profile)],
                                   'device': torch.cuda.get_device_name(), 'generator_sha256': digest(__file__)})
    cache_info = json.loads((work / 'cache.json').read_text())
    ref_dir = output / 'reference_latents'
    ref_dir.mkdir()
    paths, offset = [], 0
    for i, count in enumerate(manifest['reference']['clip_frames']):
        tensor = torch.from_numpy(np.frombuffer(payload[offset * 128:(offset + count) * 128], dtype='<f4').copy().reshape(count, 32))
        path = ref_dir / f'{i:02}.pt'
        torch.save(tensor, path)
        paths.append(str(path))
        offset += count
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('生成時間の上限です')))
    signal.alarm(manifest['pilot']['model_wall_seconds'])
    rows, runtime, current_repository = [], None, None
    try:
        for profile in selected_profiles(manifest, profile):
            if current_repository != profile['repository']:
                if runtime is not None:
                    runtime.unload()
                    runtime = None
                    torch.cuda.empty_cache()
                runtime = InferenceRuntime.from_key(RuntimeKey(
                    checkpoint=str(Path(cache_info['models'][profile['repository']]) / 'model.safetensors'),
                    codec_repo=cache_info['codec'], model_device='cuda', codec_device='cuda',
                    model_precision='fp32', codec_precision='fp32', compile_model=False))
                current_repository = profile['repository']
            is_mf = runtime.model_cfg.flow_parameterization == 'meanflow'
            if is_mf != (profile['family'] == 'meanflow') or not runtime.watermarker.ready:
                raise ValueError('モデルの方式または透かしの準備が一致しません')
            for row in texts:
                sample_id = profile['id'] + '-' + row['id']
                target = output / sample_id
                target.mkdir()
                if len(runtime.tokenizer.encode(row['text'])) > 256 or len(runtime.caption_tokenizer.encode(profile['caption'])) > 512:
                    raise ValueError('文章またはcaptionが長すぎます。切り捨てずに停止します')
                request = SamplingRequest(
                    text=row['text'], caption=profile['caption'], ref_latents=paths,
                    seed=profile['seed'], num_steps=profile['steps'], max_ref_seconds=0,
                    max_text_len=256, max_caption_len=512, duration_scale=1.0,
                    min_seconds=.5, max_seconds=30., num_candidates=1, decode_mode='sequential',
                    cfg_scale_text=3., cfg_scale_caption=4., cfg_scale_speaker=3.,
                    cfg_guidance_mode='independent', cfg_min_t=.5, cfg_max_t=1.,
                    speaker_uncond_mode='mask', t_schedule_mode='linear', context_kv_cache=True,
                    trim_tail=True, tail_window_size=20, tail_std_threshold=.05, tail_mean_threshold=.1)
                capture = {}
                decode_code = implementation_code(runtime.codec.decode_latent)
                synth_code = implementation_code(runtime.synthesize)

                def observer(frame, event, value):
                    if frame.f_code == decode_code and event == 'call':
                        if 'latent' in capture:
                            raise ValueError('複数回のdecodeが発生しました')
                        capture['latent'] = frame.f_locals['latent'].detach().float().cpu().numpy().copy()
                    if frame.f_code == synth_code and event == 'return' and value is not None:
                        for key in ('latent_steps', 'target_samples', 'pred_frames', 'scaled_frames'):
                            if key in frame.f_locals:
                                capture[key] = float(frame.f_locals[key])
                        if capture.get('scaled_frames', 0) * .04 > manifest['pilot']['max_seconds_per_utterance']:
                            raise ValueError('想定より長い生成です。次の生成へ進みません')

                save_json(target / 'started.json', {'request': vars(request), 'profile': profile['id'], 'text_id': row['id']})
                start = time.monotonic()
                sys.setprofile(observer)
                try:
                    result = runtime.synthesize(request, log_fn=print)
                finally:
                    sys.setprofile(None)
                if result.sample_rate != 48000 or result.used_seed != profile['seed'] or len(result.audios) != 1:
                    raise ValueError('予期しない生成結果です')
                pcm = result.audio.detach().cpu().float().numpy()
                if not np.isfinite(pcm).all() or not np.any(pcm):
                    raise ValueError('非有限値または無音の生成結果です')
                if 'latent' not in capture or capture['latent'].shape[0] != 1 or capture['latent'].shape[2] != 32:
                    raise ValueError('潜在表現の記録に失敗しました')
                np.save(target / 'latent.npy', capture.pop('latent'), allow_pickle=False)
                storage_gain = pcm_storage_gain(float(np.max(np.abs(pcm))))
                save_wav(target / 'teacher.wav', result.audio * storage_gain, result.sample_rate)
                record = {**row, 'text_id': row['id'], 'id': sample_id, 'profile': profile['id'], 'sample_id': sample_id,
                          'wav': sample_id + '/teacher.wav', 'latent': sample_id + '/latent.npy',
                          'wav_sha256': digest(target / 'teacher.wav'), 'latent_sha256': digest(target / 'latent.npy'),
                          'seconds': pcm.size / 48000, 'peak': float(np.max(np.abs(pcm))),
                          'clipped_samples': int(np.count_nonzero(np.abs(pcm) >= 1)),
                          'storage_gain': storage_gain,
                          'stored_peak': float(np.max(np.abs(pcm))) * storage_gain,
                          'wall_seconds': time.monotonic() - start, 'runtime_precision': 'fp32',
                          'watermark_applied': 'silentcipher_watermark' in dict(result.stage_timings),
                          'stage_timings': dict(result.stage_timings), 'capture': capture,
                          'listening_review': 'pending'}
                save_json(target / 'result.json', record)
                rows.append(record)
                print('SAMPLE_COMPLETE', sample_id, round(record['seconds'], 2), flush=True)
        save_json(output / 'dataset.json', rows)
        save_json(output / 'completed.json', {'samples': len(rows), 'quality_verified': False,
                                            'training_started': False, 'seconds': sum(r['seconds'] for r in rows)})
        print('PILOT_COMPLETE', len(rows), flush=True)
    except BaseException as exc:
        save_json(output / 'failed.json', {'error': str(exc), 'completed_samples': len(rows)})
        raise
    finally:
        signal.alarm(0)
        if runtime is not None:
            runtime.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'setup', 'cache', 'generate'])
    parser.add_argument('--work', type=Path, default=Path('/content/sumi_voice_pilot'))
    parser.add_argument('--reference', type=Path, default=HERE / 'sumi-reference-v1.sumiref')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', choices=['anime40', 'mf07', 'mf08'])
    parser.add_argument('--texts', type=Path)
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.reference = args.reference.resolve()
    if args.action == 'check':
        manifest, texts = read_plan(args.texts)
        read_reference(args.reference, manifest)
        profiles = selected_profiles(manifest, args.profile)
        print(json.dumps({'profiles': [p['id'] for p in profiles], 'texts': len(texts),
                          'samples': len(texts) * len(profiles), 'reference_verified': True}))
    elif args.action == 'setup':
        setup(args.work, args.profile)
    elif args.action == 'cache':
        cache(args.work, args.profile)
    else:
        generate(args.work, args.reference, (args.output or args.work / 'outputs').resolve(), args.profile, args.texts)


if __name__ == '__main__':
    main()
