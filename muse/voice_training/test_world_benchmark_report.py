import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from run_world_benchmark import playback_report, run


class PlaybackReportTest(unittest.TestCase):
    def test_silent_run_is_not_playback(self):
        self.assertEqual(playback_report(['playback_schedule simulated=1 minimum_start_delay=3.0s']),
                         {'audio_played': False})

    def test_actual_playback_keeps_underflow_and_latency(self):
        report = playback_report(['I (42) sumi_world_bench: speaker played=1 first_write=3.002s '
                                  'underrun_blocks=5 clipped=2 samples=79360 duration=8.150s'])
        self.assertTrue(report['audio_played'])
        self.assertEqual(report['speaker']['underrun_blocks'], 5)
        self.assertEqual(report['speaker']['clipped_samples'], 2)
        self.assertEqual(report['speaker']['written_samples'], 79360)
        self.assertEqual(report['speaker']['first_write_seconds'], 3.002)
        self.assertFalse(report['speaker']['dac_latency_measured'])

    def test_cancelled_before_output(self):
        report = playback_report(['speaker played=0 first_write=-0.000s '
                                  'underrun_blocks=0 clipped=0 samples=0 duration=1.002s'])
        self.assertFalse(report['audio_played'])

    def test_delays_follow_repetition_and_report_actual_output(self):
        class Port:
            is_open = True
            commands = []
            lines = [line for delay in (2.5, 2.0) for line in [
                b'@sumi.power ext5v=0\n',
                (f'I (42) sumi_world_bench: speaker played=1 first_write={delay:.3f}s '
                 'underrun_blocks=0 clipped=0 samples=79360 duration=8.000s\n').encode(),
                b'I (42) sumi_world_bench: result ok=1 samples=79360 rms=0.1\n',
                b'I (42) sumi_world_bench: memory monitor=1\n']]

            def write(self, data): self.commands.append(data)
            def readline(self): return self.lines.pop(0)

        with TemporaryDirectory() as directory, patch('run_world_benchmark.time.monotonic', return_value=0):
            args = SimpleNamespace(output=Path(directory) / 'measurements', port='unused',
                                   repeats=2, samples=79360, start_delays_ms=[2500, 2000])
            port = Port()
            with patch('builtins.print'):
                run(args, port)
            self.assertEqual(port.commands, [b'>sumi.tts_bench 2500\n', b'>sumi.tts_bench 2000\n'])
            report = json.loads((args.output / 'report.json').read_text())
            self.assertTrue(report['completed'] and report['audio_played'])
            self.assertEqual([r['requested_start_delay_ms'] for r in report['measurements']], [2500, 2000])
            self.assertTrue(all(r['speaker']['continuous'] for r in report['measurements']))

    def test_invalid_delay_list_rejected_before_io(self):
        args = SimpleNamespace(repeats=3, start_delays_ms=[2000])
        with self.assertRaises(ValueError):
            run(args)


if __name__ == '__main__':
    unittest.main()
