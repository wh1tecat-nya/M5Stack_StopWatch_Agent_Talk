"""Validate the BLE wire format and bounded PCM playback buffer."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PhoneAudio(unittest.TestCase):
    def test_packet_order_lengths_and_ring_wrap(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'test.c').write_text(r'''
#include "sumi_phone_audio.h"
#include <assert.h>
#include <string.h>

int main(void) {
    const uint32_t turn = 0xa0b0c0d0u;
    uint8_t frame[SUMI_PHONE_PACKET_MAX + 1] = {0};
    struct sumi_phone_packet packet;
    sumi_phone_encode_header(frame, SUMI_PHONE_REPLY_PCM, turn, 0x12345678);
    const uint8_t header[] = {7, 0xd0, 0xc0, 0xb0, 0xa0, 0x78, 0x56, 0x34, 0x12};
    assert(!memcmp(frame, header, sizeof(header)));
    assert(!sumi_phone_decode(frame, 8, &packet));
    assert(!sumi_phone_decode(frame, sizeof(frame), &packet));
    assert(sumi_phone_decode(frame, 11, &packet));
    assert(packet.type == SUMI_PHONE_REPLY_PCM && packet.offset == 0x12345678 && packet.turn == turn);
    assert(packet.length == 2 && packet.data == frame + 9);

    uint8_t storage[SUMI_PHONE_PLAYBACK_CAPACITY];
    uint8_t samples[] = {1, 2, 3, 4, 5, 6};
    uint8_t output[6];
    const uint32_t start = SUMI_PHONE_PLAYBACK_CAPACITY - 2;
    struct sumi_phone_buffer audio = {
        .bytes = storage, .turn = turn,
        .written = start, .consumed = start, .expected = start + sizeof(samples),
    };
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_PCM, turn, start, samples, 5};
    assert(!sumi_phone_buffer_append(&audio, &packet));
    packet.length = sizeof(samples);
    packet.turn++;
    assert(!sumi_phone_buffer_append(&audio, &packet));
    packet.turn = turn;
    packet.offset += 2;
    assert(!sumi_phone_buffer_append(&audio, &packet));
    packet.offset = start;
    struct sumi_phone_packet end = {SUMI_PHONE_REPLY_END, turn, audio.expected, 0, 0};
    assert(!sumi_phone_buffer_finish(&audio, &end));
    assert(sumi_phone_buffer_append(&audio, &packet));
    assert(!sumi_phone_buffer_append(&audio, &packet));
    assert(sumi_phone_buffer_finish(&audio, &end));
    assert(!sumi_phone_buffer_finish(&audio, &end));
    assert(sumi_phone_buffer_read(&audio, output, 5) == 4);
    assert(sumi_phone_buffer_read(&audio, output + 4, 2) == 2);
    assert(!memcmp(samples, output, sizeof(samples)));
    assert(!sumi_phone_buffer_read(&audio, output, sizeof(output)));

    audio = (struct sumi_phone_buffer){.bytes = storage, .turn = turn, .written = 16, .expected = SUMI_PHONE_REPLY_MAX};
    end = (struct sumi_phone_packet){SUMI_PHONE_REPLY_END, turn, 16, NULL, 0};
    assert(!sumi_phone_buffer_finish(&audio, &end));
    audio.streaming = true;
    end.offset = 14;
    assert(!sumi_phone_buffer_finish(&audio, &end));
    end.offset = 16;
    assert(sumi_phone_buffer_finish(&audio, &end));
    assert(audio.finished && audio.expected == 16);

    audio = (struct sumi_phone_buffer){.bytes = storage, .turn = turn, .expected = 2 * sizeof(storage),
                                       .written = sizeof(storage), .consumed = 0};
    packet.type = SUMI_PHONE_REPLY_PCM;
    packet.offset = sizeof(storage);
    assert(!sumi_phone_buffer_append(&audio, &packet));
    assert(sumi_phone_buffer_read(&audio, output, sizeof(output)) == sizeof(output));
    assert(sumi_phone_buffer_append(&audio, &packet));
    assert(audio.written - audio.consumed == sizeof(storage));

    static const uint8_t prefill[SUMI_PHONE_PREROLL] = {0};
    audio = (struct sumi_phone_buffer){.bytes = storage, .turn = turn};
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_BEGIN, turn, SUMI_PHONE_REPLY_PREFILL, NULL, 0};
    assert(sumi_phone_buffer_begin(&audio, &packet));
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_PCM, turn, 0, prefill, sizeof(prefill)};
    assert(sumi_phone_buffer_append(&audio, &packet));
    assert(!sumi_phone_buffer_playable(&audio, false, 640));
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_PLAY, turn + 1, 0, NULL, 0};
    assert(!sumi_phone_buffer_allow_playback(&audio, &packet));
    packet.turn = turn;
    assert(sumi_phone_buffer_allow_playback(&audio, &packet));
    assert(!sumi_phone_buffer_allow_playback(&audio, &packet));
    assert(sumi_phone_buffer_playable(&audio, false, 640));

    audio = (struct sumi_phone_buffer){.bytes = storage, .turn = turn};
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_BEGIN, turn, SUMI_PHONE_REPLY_PREFILL, NULL, 0};
    assert(sumi_phone_buffer_begin(&audio, &packet));
    packet = (struct sumi_phone_packet){SUMI_PHONE_REPLY_PCM, turn, 0, samples, sizeof(samples)};
    assert(sumi_phone_buffer_append(&audio, &packet));
    assert(!sumi_phone_buffer_playable(&audio, false, 640));
    end.offset = sizeof(samples);
    assert(sumi_phone_buffer_finish(&audio, &end));
    assert(sumi_phone_buffer_playable(&audio, false, 640));
}
''')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(ROOT / 'overlay'), str(work / 'test.c'),
                            str(ROOT / 'overlay/sumi_phone_audio.c'), '-o', str(work / 'test')], check=True)
            subprocess.run([str(work / 'test')], check=True)
