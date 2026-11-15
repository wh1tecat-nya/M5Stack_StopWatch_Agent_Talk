"""Run the SDK's overflow/order harness with this build's actual backlog size."""
import importlib.util
import re
import unittest
from test_controls import SDK

spec = importlib.util.spec_from_file_location('sdk_backlog', SDK / 'tests/test_muse_voice_note_backlog.py')
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)


class AudioBacklog(upstream.PcmStreamBufferTest):
    def run_harness(self, code):
        source = (SDK / 'components/muse/muse_chat_session.cpp').read_text()
        branch = source.split('#if CONFIG_MUSE_HATCH_COMPACT_BUFFERS', 1)[1].split('#else', 1)[1].split('#endif', 1)[0]
        expression = re.search(r'^#define IN_BYTES (.+?)(?:/\*|$)', branch, re.M).group(1).strip()
        self.assertIn('MIC_RATE', expression)
        code, count = re.subn(r'^#define IN_BYTES .+$', '#define MIC_RATE 16000\n#define IN_BYTES ' + expression, code, flags=re.M)
        self.assertEqual(count, 1)
        super().run_harness(code)


if __name__ == '__main__':
    unittest.main()
