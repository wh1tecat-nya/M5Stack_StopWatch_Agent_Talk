"""Check the Java client against the pinned Muse SDK's independent responder."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ANDROID = Path(__file__).resolve().parents[1]
SDK = ANDROID.parent / '.cache/muse-gadget-sdk-463bd2a05380fea4211b0099ad0f70933c108b20'
sys.path.insert(0, str(SDK / 'linux/src'))

from musegadget.noise.envelope import ApplicationResponse, ServiceFrame
from musegadget.noise.framing import NoiseFrameDecoder, encode_noise_frames
from musegadget.noise.noise_xx import NoiseXXResponder
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope


class MuseInterop(unittest.TestCase):
    def test_context_footer(self):
        java_home = os.environ.get('JAVA_HOME')
        java = Path(java_home, 'bin/java') if java_home else 'java'
        javac = Path(java_home, 'bin/javac') if java_home else 'javac'
        with tempfile.TemporaryDirectory() as classes:
            subprocess.run([javac, '--release', '17', '-d', classes,
                            ANDROID / 'src/jp/sumi/stopwatch/MuseReplyText.java',
                            ANDROID / 'tests/MuseReplyTextTest.java'], check=True)
            subprocess.run([java, '-cp', classes, 'jp.sumi.stopwatch.MuseReplyTextTest'], check=True)

    def test_sdk_responder_and_chat_stream(self):
        java_home = os.environ.get('JAVA_HOME')
        java = Path(java_home, 'bin/java') if java_home else 'java'
        javac = Path(java_home, 'bin/javac') if java_home else 'javac'
        sources = ANDROID / 'src/jp/sumi/stopwatch'
        with tempfile.TemporaryDirectory() as classes:
            subprocess.run([javac, '--release', '17', '-d', classes, sources / 'MuseNoise.java',
                            sources / 'MuseWire.java', ANDROID / 'tests/SumiMuseInterop.java'], check=True)
            with subprocess.Popen([java, '-cp', classes, 'jp.sumi.stopwatch.SumiMuseInterop'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
                try:
                    def read():
                        row = process.stdout.readline()
                        self.assertTrue(row)
                        return base64.b64decode(row)

                    def send(data):
                        process.stdin.write(base64.b64encode(data) + b'\n')
                        process.stdin.flush()

                    peer = NoiseXXResponder()
                    peer.initialize()
                    send(peer.read_message1_and_write_message2(read()))
                    peer.read_message3(read())
                    enc, dec = peer.split()
                    remote = decode_request_envelope(NoiseFrameDecoder().decode(dec.decrypt_with_ad(b'', read())))
                    self.assertEqual((remote.stream_id, remote.value.verb, remote.value.path), (2, 'POST', '/chat/stream'))
                    self.assertEqual(json.loads(remote.value.body),
                                     {'message': 'こんにちは', 'output_modality': 'text', 'device': 'mcu-wearable'})
                    body = b'{"message_id":"example"}'
                    frame = ServiceFrame.response(2, ApplicationResponse(200, [], body, True))
                    send(enc.encrypt_with_ad(b'', encode_noise_frames(encode_response_envelope(frame))[0]))
                    self.assertEqual(read(), body)
                    self.assertEqual(process.wait(timeout=5), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
