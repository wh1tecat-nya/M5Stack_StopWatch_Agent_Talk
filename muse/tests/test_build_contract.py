"""Check the distributable layout, pinned inputs, and source integration."""
from pathlib import Path
import hashlib
import importlib.util
import unittest

ROOT = Path(__file__).resolve().parents[1]

class BuildContract(unittest.TestCase):
    def test_partition_ranges(self):
        regions = {}
        for line in (ROOT / 'partitions.csv').read_text().splitlines():
            if not line or line.startswith('#'): continue
            name, _, _, offset, size, *_ = [s.strip() for s in line.split(',')]
            regions[name] = (int(offset, 0), int(size, 0))
        ends = sorted((start, start + size, name) for name, (start, size) in regions.items())
        for before, after in zip(ends, ends[1:]): self.assertLessEqual(before[1], after[0])
        self.assertLessEqual(ends[-1][1], 16 * 1024 * 1024)
        self.assertEqual(regions['prod_data'], (0x820000, 0x1000))
        self.assertEqual(regions['prod_bak'], (0x821000, 0x1000))
        self.assertLessEqual((ROOT / '.cache/dict.bin').stat().st_size, regions['dict'][1])

    def test_download_checksums(self):
        spec = importlib.util.spec_from_file_location('prepare', ROOT / 'prepare.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for name, (_, digest) in module.ASSETS.items():
            self.assertEqual(hashlib.sha256((ROOT / '.cache' / name).read_bytes()).hexdigest(), digest)

    def test_build_selects_both_voices_and_safe_flash_layout(self):
        config = (ROOT / 'build/sdkconfig').read_text()
        for setting in ['CONFIG_MUSE_BOARD_M5STACK_STOPWATCH=y', 'CONFIG_MUSE_REALTIME_VOICE=y',
                        'CONFIG_MUSE_CJK_FONT=y', 'CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y',
                        'CONFIG_MUSE_STATIC_BUFFERS_IN_PSRAM=y',
                        'CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=0']:
            self.assertIn(setting, config)
        self.assertNotIn('\nCONFIG_HOMEHUB_OTA_ENABLED=y', config)
        self.assertNotIn('\nCONFIG_SECURE_BOOT=y', config)

if __name__ == '__main__': unittest.main()
