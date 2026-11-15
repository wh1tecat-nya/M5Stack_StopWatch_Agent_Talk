#include "sumi_phone_audio.h"
#include <string.h>

static uint32_t load_le32(const uint8_t *src)
{
    return (uint32_t)src[0] | ((uint32_t)src[1] << 8) |
           ((uint32_t)src[2] << 16) | ((uint32_t)src[3] << 24);
}

static void store_le32(uint8_t *dest, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) dest[i] = (uint8_t)(value >> (i * 8));
}

bool sumi_phone_decode(const uint8_t *bytes, size_t length, struct sumi_phone_packet *packet)
{
    if (length < SUMI_PHONE_HEADER || length > SUMI_PHONE_PACKET_MAX) return false;
    *packet = (struct sumi_phone_packet){
        .type = bytes[0], .turn = load_le32(bytes + 1), .offset = load_le32(bytes + 5),
        .data = bytes + SUMI_PHONE_HEADER, .length = length - SUMI_PHONE_HEADER,
    };
    return true;
}

void sumi_phone_encode_header(uint8_t *bytes, uint8_t type, uint32_t turn, uint32_t offset)
{
    bytes[0] = type;
    store_le32(bytes + 1, turn);
    store_le32(bytes + 5, offset);
}

bool sumi_phone_buffer_append(struct sumi_phone_buffer *buffer, const struct sumi_phone_packet *packet)
{
    if (!buffer->bytes || !buffer->expected || buffer->finished || packet->turn != buffer->turn ||
        packet->type != SUMI_PHONE_REPLY_PCM || packet->offset != buffer->written ||
        !packet->length || (packet->length & 1) || packet->length > buffer->expected - buffer->written ||
        packet->length > SUMI_PHONE_PLAYBACK_CAPACITY - (buffer->written - buffer->consumed)) return false;
    size_t position = buffer->written % SUMI_PHONE_PLAYBACK_CAPACITY;
    size_t first = SUMI_PHONE_PLAYBACK_CAPACITY - position;
    if (first > packet->length) first = packet->length;
    memcpy(buffer->bytes + position, packet->data, first);
    memcpy(buffer->bytes, packet->data + first, packet->length - first);
    buffer->written += packet->length;
    return true;
}

bool sumi_phone_buffer_finish(struct sumi_phone_buffer *buffer, const struct sumi_phone_packet *packet)
{
    if (!buffer->bytes || !buffer->expected || buffer->finished || packet->turn != buffer->turn ||
        packet->type != SUMI_PHONE_REPLY_END || packet->length || packet->offset != buffer->written ||
        !buffer->written || buffer->written > buffer->expected ||
        (!buffer->streaming && buffer->written != buffer->expected)) return false;
    buffer->expected = buffer->written;
    buffer->finished = true;
    return true;
}

size_t sumi_phone_buffer_read(struct sumi_phone_buffer *buffer, void *output, size_t length)
{
    size_t available = buffer->written - buffer->consumed;
    if (length > available) length = available;
    length &= ~(size_t)1;
    if (!length) return 0;
    size_t position = buffer->consumed % SUMI_PHONE_PLAYBACK_CAPACITY;
    size_t first = SUMI_PHONE_PLAYBACK_CAPACITY - position;
    if (first > length) first = length;
    memcpy(output, buffer->bytes + position, first);
    memcpy((uint8_t *)output + first, buffer->bytes, length - first);
    buffer->consumed += length;
    return length;
}
