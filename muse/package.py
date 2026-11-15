#!/usr/bin/env python3
"""Package separate images; never fill/overwrite the product-data sectors."""
import hashlib
import json
import os
import shutil
from pathlib import Path
from prepare import ROOT, ESP, REV, ASSETS

def package():
    build = Path(os.environ.get('SUMI_BUILD_DIR', ROOT / 'build')).resolve()
    dest = Path(os.environ.get('SUMI_DIST_DIR', ROOT / 'dist')).resolve()
    config = (build / 'sdkconfig').read_text()
    required = ['CONFIG_MUSE_BOARD_M5STACK_STOPWATCH=y', 'CONFIG_MUSE_REALTIME_VOICE=y',
                'CONFIG_MUSE_CJK_FONT=y', 'CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y',
                'CONFIG_MUSE_STATIC_BUFFERS_IN_PSRAM=y',
                'CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=0']
    if any(s not in config for s in required) or '\nCONFIG_SECURE_BOOT=y' in config:
        raise RuntimeError('Unexpected target/security configuration; package aborted')
    dest.mkdir(parents=True, exist_ok=True)
    source_files = [(0, build / 'bootloader/bootloader.bin', 'bootloader.bin', 0x10000),
        (0x10000, build / 'partition_table/partition-table.bin', 'partition-table.bin', 0x1000),
        (0x1D000, build / 'ota_data_initial.bin', 'ota_data_initial.bin', 0x2000),
        (0x20000, build / 'muse-gadget.bin', 'sumi-muse.bin', 0x800000),
        (0x830000, ROOT / '.cache/dict.bin', 'dict.bin', 0x680000)]
    manifest = {'sdk_revision': REV, 'idf_version': '6.0.3', 'chip': 'esp32s3', 'flash_size': '16MB',
        'status': 'built; flashing and pairing are separate steps', 'images': [],
        'preserve': ['prod_data at 0x820000', 'prod_bak at 0x821000'],
        'migration': 'Back up the entire 16MB flash first. New NVS layout requires initialization. See README.md.'}
    for address, source, name, maximum in source_files:
        data = source.read_bytes()
        if len(data) > maximum: raise RuntimeError(f'{name} exceeds its partition')
        if name == 'dict.bin' and hashlib.sha256(data).hexdigest() != ASSETS['dict.bin'][1]:
            raise RuntimeError('Dictionary checksum mismatch')
        shutil.copy2(source, dest / name)
        manifest['images'].append({'file': name, 'address': hex(address), 'bytes': len(data),
                                   'sha256': hashlib.sha256(data).hexdigest()})
    (dest / 'flash-plan.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (dest / 'SHA256SUMS').write_text(''.join(f"{x['sha256']}  {x['file']}\n" for x in manifest['images']))
    for name in ['README.md', 'THIRD_PARTY_NOTICES.md', 'VALIDATION.md']:
        if (ROOT / name).exists(): shutil.copy2(ROOT / name, dest / name)
    licenses = dest / 'licenses'; licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / '.cache/NotoSansCJK-OFL.txt', licenses / 'NotoSansCJK-OFL.txt')
    shutil.copy2(ESP.parent / 'LICENSE', licenses / 'Muse-SDK-Apache-2.0.txt')
    for group in ['SanoTTS-jp', 'SanoTTS-jp-voice-tsukuyomi-v4']:
        for file in (ESP / 'components/sanotts' / group).iterdir():
            if file.is_file() and (file.name.startswith('LICENSE') or file.name.startswith('NOTICE')):
                shutil.copy2(file, licenses / (group + '-' + file.name))
    if os.environ.get('SUMI_WORLD_BENCH') == '1':
        shutil.copy2(ESP / 'components/sumi_world_bench/WORLD-LICENSE.txt', licenses / 'WORLD-BSD-3-Clause.txt')
        if os.environ.get('SUMI_WORLD_ESP_DSP') == '1':
            shutil.copy2(ESP / 'components/esp-dsp/LICENSE', licenses / 'ESP-DSP-Apache-2.0.txt')
        (dest / 'WORLD-DIAGNOSTIC.txt').write_text(
            'Local benchmark build. >sumi.tts_bench measures the fixed WORLD pipeline.\n'
            'No text frontend, microphone, network request, speaker playback, or NVS changes in this benchmark.\n'
            'Contains private voice weights and a teacher latent fixture; do not publish.\n')
    print(f'Packaged firmware in {dest} (no device writes)')

if __name__ == '__main__': package()
