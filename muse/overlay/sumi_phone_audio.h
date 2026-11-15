#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Version 1 carries mono 16 kHz signed little-endian PCM in both directions. */
#define SUMI_PHONE_VERSION 1
#define SUMI_PHONE_HEADER 9
#define SUMI_PHONE_PACKET_MAX 253
#define SUMI_PHONE_UPLOAD_WINDOW 4096
#define SUMI_PHONE_PLAYBACK_CAPACITY (64 * 1024)
#define SUMI_PHONE_PREROLL (400 * 32)
#define SUMI_PHONE_NOTE_MAX (15 * 32000)
#define SUMI_PHONE_REPLY_MAX (120 * 32000)
#define SUMI_PHONE_REPLY_STREAM UINT32_MAX
#define SUMI_PHONE_FEATURE_STREAM 1u

enum sumi_phone_message {
    SUMI_PHONE_HELLO = 1,
    SUMI_PHONE_NOTE_BEGIN,
    SUMI_PHONE_NOTE_PCM,
    SUMI_PHONE_NOTE_END,
    SUMI_PHONE_NOTE_ACK,
    SUMI_PHONE_REPLY_BEGIN,
    SUMI_PHONE_REPLY_PCM,
    SUMI_PHONE_REPLY_END,
    SUMI_PHONE_CANCEL,
    SUMI_PHONE_ERROR,
    SUMI_PHONE_REPLY_STATUS,
    SUMI_PHONE_CAPTION,
    SUMI_PHONE_GENERATING,
};

enum sumi_phone_setting {
    SUMI_SETTING_MODE, SUMI_SETTING_CHAT, SUMI_SETTING_WEARABLE, SUMI_SETTING_VOLUME,
    SUMI_SETTING_MIC_GAIN, SUMI_SETTING_TTS_RATE, SUMI_SETTING_WIFI_ASR, SUMI_SETTING_AVATAR_LIGHT,
    SUMI_SETTING_BADGE_LIGHT, SUMI_SETTING_AVATAR_TIME, SUMI_SETTING_BADGE_TIME, SUMI_SETTING_DIM_TIME,
    SUMI_SETTING_CHARGE, SUMI_SETTING_WIFI_SETUP, SUMI_SETTING_COUNT,
};

struct sumi_phone_packet {
    uint8_t type;
    uint32_t turn;
    uint32_t offset;
    const uint8_t *data;
    size_t length;
};

bool sumi_phone_decode(const uint8_t *bytes, size_t length, struct sumi_phone_packet *packet);
void sumi_phone_encode_header(uint8_t *bytes, uint8_t type, uint32_t turn, uint32_t offset);

/* The voice task owns allocation and release. BLE access and playback serialize
 * operations on this state. Offsets always refer to bytes in the whole clip. */
struct sumi_phone_buffer {
    uint8_t *bytes;
    uint32_t turn;
    uint32_t written;
    uint32_t consumed;
    uint32_t expected;
    bool finished;
    bool streaming;
};

bool sumi_phone_buffer_append(struct sumi_phone_buffer *buffer, const struct sumi_phone_packet *packet);
bool sumi_phone_buffer_finish(struct sumi_phone_buffer *buffer, const struct sumi_phone_packet *packet);
size_t sumi_phone_buffer_read(struct sumi_phone_buffer *buffer, void *output, size_t length);
