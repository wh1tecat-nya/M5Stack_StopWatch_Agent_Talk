import hashlib
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from flash_world_benchmark import APP_BYTES, APP_OFFSET, experiment


class FakeDevice:
    def __init__(self, fail=None):
        self.port = object()
        self.fail = fail
        self.writes = []
        self.closed = False

    def connect(self):
        pass

    def check_original(self, backup, table):
        if self.fail == 'preflight':
            raise ValueError('Different current app')

    def write(self, data):
        self.writes.append(data)
        if (self.fail == 'candidate' and len(self.writes) == 1) or (self.fail == 'restore' and len(self.writes) == 2):
            raise RuntimeError('Simulated interrupted write')

    def boot(self):
        pass

    def close(self):
        self.closed = True


class FlashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.args = SimpleNamespace(port='unused', repeats=3, output=root / 'run')
        self.backup = b'\xe9' + bytes(APP_BYTES - 1)
        table = bytearray(b'\xff' * 4096)
        struct.pack_into('<HBBII', table, 0, 0x50aa, 0, 0, APP_OFFSET, APP_BYTES)
        for name, data in [('backup', self.backup), ('candidate', b'\xe9' + bytes(1000000)), ('table', table)]:
            path = root / name
            path.write_bytes(data)
            setattr(self.args, name, path)
            setattr(self.args, name + '_sha256', hashlib.sha256(data).hexdigest())

    def call(self, device, measure=None, health=None):
        def normal_measure(args, port):
            self.assertIs(port, device.port)
            self.assertEqual(args.samples, 109368)
        return experiment(self.args, device, measure or normal_measure,
                          health or (lambda port, output: {'console_responses': 7}))

    def test_success_retains_port_and_restores_full_partition(self):
        device = FakeDevice()
        result = self.call(device)
        self.assertTrue(result['benchmark_completed'])
        self.assertTrue(result['original_restored'])
        self.assertEqual(len(device.writes), 2)
        self.assertEqual(device.writes[-1], self.backup)
        self.assertTrue(device.closed)

    def test_partial_candidate_write_still_restores(self):
        device = FakeDevice('candidate')
        result = self.call(device)
        self.assertFalse(result['benchmark_completed'])
        self.assertTrue(result['original_restored'])
        self.assertEqual(device.writes[-1], self.backup)

    def test_interrupted_measurement_still_restores(self):
        def interrupted(*args):
            raise KeyboardInterrupt()
        device = FakeDevice()
        result = self.call(device, measure=interrupted)
        self.assertTrue(result['original_restored'])
        self.assertIn('KeyboardInterrupt', result['benchmark_error'])
        self.assertEqual(device.writes[-1], self.backup)

    def test_wrong_current_app_never_written(self):
        device = FakeDevice('preflight')
        self.call(device)
        self.assertEqual(device.writes, [])
        self.assertTrue(device.closed)

    def test_failed_restore_not_reported_as_restored(self):
        device = FakeDevice('restore')
        result = self.call(device)
        self.assertFalse(result['original_restored'])
        self.assertIn('restoration_error', result)
        self.assertTrue(device.closed)

    def test_failed_health_does_not_erase_successful_flash(self):
        def failed(*args):
            raise RuntimeError('No serial response')
        result = self.call(FakeDevice(), health=failed)
        self.assertTrue(result['original_restored'])
        self.assertNotIn('restored_health', result)
        self.assertIn('restoration_error', result)

    def test_invalid_backup_rejected_before_device_access(self):
        device = FakeDevice()
        self.args.backup.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.call(device)
        self.assertEqual(device.writes, [])
        self.assertFalse(self.args.output.exists())


if __name__ == '__main__':
    unittest.main()
