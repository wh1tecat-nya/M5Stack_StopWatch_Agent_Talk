"""検証済みのEspressif公式DSPを固定版で準備する。"""
import hashlib
from pathlib import Path
import tarfile
import urllib.request

REVISION = '3c8ac0fdfec83740b783e200862c8d0c056de0ad'
ARCHIVE_SHA = '2b2052c80ddef8552bbee5316175021d66d91e2cbcc5de5f87aa270ed24f6196'


def prepare_dsp():
    cache = Path(__file__).resolve().parent.parent / '.cache'
    name = 'esp-dsp-' + REVISION
    archive = cache / (name + '.tar.gz')
    if not archive.exists():
        with urllib.request.urlopen('https://codeload.github.com/espressif/esp-dsp/tar.gz/' + REVISION, timeout=60) as src:
            data = src.read()
        if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA:
            raise ValueError('ESP-DSP download checksum mismatch')
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise ValueError('ESP-DSP archive checksum mismatch')
    with tarfile.open(archive) as src:
        if not all(m.name == name or m.name.startswith(name + '/') for m in src.getmembers()):
            raise ValueError('Unexpected ESP-DSP archive path')
        # Restore source from the verified archive, avoiding trust in stale edits.
        src.extractall(cache, filter='data')
    return cache / name
