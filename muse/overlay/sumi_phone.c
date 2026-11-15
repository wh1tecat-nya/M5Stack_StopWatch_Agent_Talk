#include "sumi_phone.h"
#include "sumi_phone_audio.h"

#include "muse_audio.h"
#include "muse_ble.h"
#include "muse_state.h"
#include "muse_wifi.h"
#include "sumi_chat.h"
#include "sumi_ui.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "nvs.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#define WINDOW_TIMEOUT_US (5 * 1000000LL)
#define REPLY_TIMEOUT_US (90 * 1000000LL)
#define STREAM_WAIT_US (40 * 1000000LL)
#define STREAM_TOTAL_US (180 * 1000000LL)
#define PCM_BYTES (MUSE_AUDIO_CHUNK * sizeof(int16_t))

static atomic_bool phone_mode;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static uint16_t conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool subscribed;
static bool hello;
static uint32_t next_turn;
static uint32_t sent;
static uint32_t acked;
static bool note_finished;
static bool phone_error;
static int64_t generation_wait_until;
static char caption[236];
static bool caption_pending;
static struct sumi_phone_buffer reply;
uint16_t sumi_phone_tx_handle;

static atomic_bool settings_enabled;
static uint16_t settings_values[SUMI_SETTING_COUNT];
static uint16_t setting_request;
static uint16_t setting_sequence;
static uint16_t setting_value;
static uint16_t setting_conn;
static uint8_t setting_key;
static uint8_t setting_result;
static bool setting_pending;
static TickType_t settings_until;

bool sumi_phone_settings_enabled(void)
{
    return atomic_load(&settings_enabled);
}

void sumi_phone_load_mode(void)
{
    nvs_handle_t nvs;
    int32_t mode = 0;
    if (nvs_open("sumi-muse", NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_i32(nvs, "phone_mode", &mode);
        nvs_close(nvs);
    }
    atomic_store(&phone_mode, mode == 1);
}

bool sumi_phone_mode(void)
{
    return atomic_load(&phone_mode);
}

bool sumi_phone_set_mode(bool enabled)
{
    if (sumi_phone_mode() == enabled) return true;
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READWRITE, &nvs) != ESP_OK) return false;
    esp_err_t result = nvs_set_i32(nvs, "phone_mode", enabled);
    if (result == ESP_OK) result = nvs_commit(nvs);
    nvs_close(nvs);
    if (result != ESP_OK) return false;
    portENTER_CRITICAL(&lock);
    atomic_store(&phone_mode, enabled);
    hello = false;
    if (reply.turn) phone_error = true;
    portEXIT_CRITICAL(&lock);
    muse_ble_apply();
    muse_wifi_apply();
    return true;
}

static bool ready_locked(void)
{
    return sumi_phone_mode() && conn_handle != BLE_HS_CONN_HANDLE_NONE && subscribed && hello;
}

bool sumi_phone_ready(void)
{
    portENTER_CRITICAL(&lock);
    bool ready = ready_locked();
    portEXIT_CRITICAL(&lock);
    return ready;
}

uint32_t sumi_phone_begin(void)
{
    uint32_t seed = esp_random();
    portENTER_CRITICAL(&lock);
    uint32_t turn = 0;
    if (ready_locked() && !reply.turn && !setting_pending) {
        if (!next_turn) next_turn = seed;
        if (++next_turn == 0) ++next_turn;
        turn = next_turn;
        reply = (struct sumi_phone_buffer){.turn = turn};
        sent = acked = 0;
        note_finished = phone_error = false;
        generation_wait_until = 0;
        caption_pending = false;
        caption[0] = '\0';
    }
    portEXIT_CRITICAL(&lock);
    return turn;
}

bool sumi_phone_current(uint32_t turn)
{
    portENTER_CRITICAL(&lock);
    bool valid = turn && reply.turn == turn && ready_locked() && !phone_error;
    portEXIT_CRITICAL(&lock);
    return valid;
}

static int secure_connection(uint16_t conn)
{
    struct ble_gap_conn_desc desc;
    return ble_gap_conn_find(conn, &desc) == 0 && desc.sec_state.encrypted &&
           desc.sec_state.authenticated && desc.sec_state.bonded;
}

static bool notify(uint8_t type, uint32_t turn, uint32_t offset, const void *data, size_t length)
{
    portENTER_CRITICAL(&lock);
    uint16_t conn = ready_locked() && reply.turn == turn ? conn_handle : BLE_HS_CONN_HANDLE_NONE;
    portEXIT_CRITICAL(&lock);
    if (conn == BLE_HS_CONN_HANDLE_NONE || !secure_connection(conn)) return false;
    uint8_t bytes[SUMI_PHONE_PACKET_MAX];
    if (length > sizeof(bytes) - SUMI_PHONE_HEADER) return false;
    sumi_phone_encode_header(bytes, type, turn, offset);
    if (length) memcpy(bytes + SUMI_PHONE_HEADER, data, length);
    struct os_mbuf *packet = ble_hs_mbuf_from_flat(bytes, SUMI_PHONE_HEADER + length);
    return packet && ble_gatts_notify_custom(conn, sumi_phone_tx_handle, packet) == 0;
}

void sumi_phone_end(uint32_t turn, bool cancel)
{
    if (cancel) notify(SUMI_PHONE_CANCEL, turn, 0, NULL, 0);
    portENTER_CRITICAL(&lock);
    uint8_t *buffer = NULL;
    if (reply.turn == turn) {
        buffer = reply.bytes;
        reply = (struct sumi_phone_buffer){0};
    }
    portEXIT_CRITICAL(&lock);
    free(buffer);
}

void sumi_phone_gap_event(struct ble_gap_event *event)
{
    portENTER_CRITICAL(&lock);
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            conn_handle = event->connect.conn_handle;
            subscribed = hello = false;
            if (reply.turn) phone_error = true;
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        if (event->disconnect.conn.conn_handle == conn_handle) {
            conn_handle = BLE_HS_CONN_HANDLE_NONE;
            setting_conn = BLE_HS_CONN_HANDLE_NONE;
            subscribed = hello = false;
            if (reply.turn) phone_error = true;
        }
        break;
    case BLE_GAP_EVENT_ENC_CHANGE:
        if (event->enc_change.conn_handle == conn_handle && event->enc_change.status != 0) {
            hello = false;
            if (reply.turn) phone_error = true;
        }
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.conn_handle == conn_handle && event->subscribe.attr_handle == sumi_phone_tx_handle) {
            subscribed = event->subscribe.cur_notify;
            if (!subscribed) {
                hello = false;
                if (reply.turn) phone_error = true;
            }
        }
        break;
    }
    portEXIT_CRITICAL(&lock);
}

static bool accept_locked(const struct sumi_phone_packet *packet)
{
    if (packet->type == SUMI_PHONE_HELLO) {
        if (!subscribed || reply.turn || packet->turn || packet->length || packet->offset != SUMI_PHONE_VERSION) return false;
        hello = true;
        return true;
    }
    if (!ready_locked() || !reply.turn || packet->turn != reply.turn || phone_error) return false;
    switch (packet->type) {
    case SUMI_PHONE_NOTE_ACK:
        if (packet->length || packet->offset < acked || packet->offset > sent || (packet->offset & 1)) return false;
        acked = packet->offset;
        return true;
    case SUMI_PHONE_REPLY_BEGIN:
        if (!note_finished || !reply.bytes || reply.expected || packet->length || !packet->offset ||
            (packet->offset != SUMI_PHONE_REPLY_STREAM &&
             (packet->offset > SUMI_PHONE_REPLY_MAX || (packet->offset & 1)))) return false;
        reply.streaming = packet->offset == SUMI_PHONE_REPLY_STREAM;
        reply.expected = reply.streaming ? SUMI_PHONE_REPLY_MAX : packet->offset;
        return true;
    case SUMI_PHONE_REPLY_PCM:
        return sumi_phone_buffer_append(&reply, packet);
    case SUMI_PHONE_REPLY_END:
        return sumi_phone_buffer_finish(&reply, packet);
    case SUMI_PHONE_CAPTION:
        if (!note_finished || !packet->length || packet->length >= sizeof(caption) ||
            memchr(packet->data, 0, packet->length)) return false;
        memcpy(caption, packet->data, packet->length);
        caption[packet->length] = 0;
        caption_pending = true;
        return true;
    case SUMI_PHONE_GENERATING:
        if (!note_finished || packet->length || packet->offset || reply.finished) return false;
        generation_wait_until = esp_timer_get_time() + STREAM_WAIT_US;
        return true;
    case SUMI_PHONE_ERROR:
        if (packet->length) return false;
        phone_error = true;
        return true;
    default:
        return false;
    }
}

int sumi_phone_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)attr;
    (void)arg;
    if (!sumi_phone_mode() || !secure_connection(conn)) return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        uint8_t status[SUMI_PHONE_HEADER + 4];
        portENTER_CRITICAL(&lock);
        uint32_t capacity = SUMI_PHONE_PLAYBACK_CAPACITY - (reply.written - reply.consumed);
        sumi_phone_encode_header(status, SUMI_PHONE_REPLY_STATUS, reply.turn,
                                 reply.turn ? reply.written : SUMI_PHONE_FEATURE_STREAM);
        portEXIT_CRITICAL(&lock);
        for (unsigned i = 0; i < 4; ++i) status[SUMI_PHONE_HEADER + i] = (uint8_t)(capacity >> (8 * i));
        return os_mbuf_append(ctxt->om, status, sizeof(status)) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
    }
    uint8_t bytes[SUMI_PHONE_PACKET_MAX];
    uint16_t length = 0;
    struct sumi_phone_packet packet;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR ||
        ble_hs_mbuf_to_flat(ctxt->om, bytes, sizeof(bytes), &length) ||
        !sumi_phone_decode(bytes, length, &packet)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    portENTER_CRITICAL(&lock);
    bool accepted = conn_handle == conn && sumi_phone_mode() && accept_locked(&packet);
    portEXIT_CRITICAL(&lock);
    return accepted ? 0 : BLE_ATT_ERR_UNLIKELY;
}

static void store16(uint8_t *out, uint16_t value)
{
    out[0] = value & 255;
    out[1] = value >> 8;
}

static uint16_t read16(const uint8_t *in)
{
    return in[0] | (uint16_t)in[1] << 8;
}

int sumi_phone_settings_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)attr;
    (void)arg;
    if ((!sumi_phone_mode() && !sumi_phone_settings_enabled()) || !secure_connection(conn))
        return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        uint8_t bytes[5 + SUMI_SETTING_COUNT * 2];
        portENTER_CRITICAL(&lock);
        bytes[0] = 1;
        store16(bytes + 1, setting_sequence);
        bytes[3] = setting_result;
        bytes[4] = SUMI_SETTING_COUNT;
        for (unsigned i = 0; i < SUMI_SETTING_COUNT; ++i) store16(bytes + 5 + i * 2, settings_values[i]);
        portEXIT_CRITICAL(&lock);
        return os_mbuf_append(ctxt->om, bytes, sizeof(bytes)) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
    }
    uint8_t command[6];
    uint16_t length = 0;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR ||
        ble_hs_mbuf_to_flat(ctxt->om, command, sizeof(command), &length) || length != sizeof(command) ||
        command[0] != 1 || command[3] >= SUMI_SETTING_COUNT)
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    portENTER_CRITICAL(&lock);
    bool accepted = conn_handle == conn && !setting_pending && read16(command + 1) != 0;
    if (accepted) {
        setting_request = read16(command + 1);
        setting_key = command[3];
        setting_value = read16(command + 4);
        setting_conn = conn;
        setting_pending = true;
    }
    portEXIT_CRITICAL(&lock);
    return accepted ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

void sumi_phone_open_settings(void)
{
    settings_until = xTaskGetTickCount() + pdMS_TO_TICKS(5 * 60 * 1000);
    atomic_store(&settings_enabled, true);
    muse_ble_apply();
}

void sumi_phone_apply_setting(void)
{
    if (sumi_phone_settings_enabled() && (int32_t)(xTaskGetTickCount() - settings_until) >= 0) {
        atomic_store(&settings_enabled, false);
        muse_ble_apply();
    }
    portENTER_CRITICAL(&lock);
    bool pending = setting_pending;
    bool disconnected = conn_handle == BLE_HS_CONN_HANDLE_NONE || setting_conn != conn_handle;
    bool busy = reply.turn != 0;
    unsigned key = setting_key;
    int value = setting_value;
    portEXIT_CRITICAL(&lock);
    // Keep a short-lived connection for the readback after switching back to Wi-Fi.
    int result = 2;
    if (pending && !disconnected && !busy) {
        if (key == SUMI_SETTING_MODE && !value && sumi_phone_mode()) sumi_phone_open_settings();
        result = sumi_ui_apply_setting(key, value);
    }
    uint16_t latest[SUMI_SETTING_COUNT];
    for (unsigned i = 0; i < SUMI_SETTING_COUNT; ++i) latest[i] = sumi_ui_setting(i);
    portENTER_CRITICAL(&lock);
    memcpy(settings_values, latest, sizeof(latest));
    if (pending) {
        setting_result = (uint8_t)result;
        setting_sequence = setting_request;
        setting_pending = false;
    }
    portEXIT_CRITICAL(&lock);
}

static bool active(uint32_t turn, bool (*interrupted)(void))
{
    muse_state_poke();
    return !interrupted() && sumi_phone_current(turn);
}

static bool send_note(uint32_t turn, const uint8_t *pcm, uint32_t bytes, bool (*interrupted)(void))
{
    uint8_t options[] = {sumi_chat_selected(), sumi_chat_wearable()};
    int64_t deadline = esp_timer_get_time() + WINDOW_TIMEOUT_US;
    while (active(turn, interrupted)) {
        if (notify(SUMI_PHONE_NOTE_BEGIN, turn, bytes, options, sizeof(options))) break;
        if (esp_timer_get_time() >= deadline) return false;
        vTaskDelay(1);
    }
    uint32_t offset = 0;
    uint32_t previous_ack = 0;
    while (active(turn, interrupted)) {
        portENTER_CRITICAL(&lock);
        uint16_t conn = conn_handle;
        uint32_t acknowledged = acked;
        portEXIT_CRITICAL(&lock);
        uint32_t mtu = ble_att_mtu(conn);
        if (mtu < 3 + SUMI_PHONE_HEADER + 2) return false;
        if (acknowledged != previous_ack) {
            previous_ack = acknowledged;
            deadline = esp_timer_get_time() + WINDOW_TIMEOUT_US;
        }
        if (acknowledged == bytes) {
            portENTER_CRITICAL(&lock);
            note_finished = true;
            portEXIT_CRITICAL(&lock);
            if (notify(SUMI_PHONE_NOTE_END, turn, bytes, NULL, 0)) return true;
        } else if (offset < bytes && offset - acknowledged < SUMI_PHONE_UPLOAD_WINDOW) {
            size_t capacity = (mtu - 3 < SUMI_PHONE_PACKET_MAX ? mtu - 3 : SUMI_PHONE_PACKET_MAX) - SUMI_PHONE_HEADER;
            size_t length = bytes - offset;
            if (length > capacity) length = capacity;
            size_t credit = SUMI_PHONE_UPLOAD_WINDOW - (offset - acknowledged);
            if (length > credit) length = credit;
            length &= ~(size_t)1;
            portENTER_CRITICAL(&lock);
            sent = offset + length;
            portEXIT_CRITICAL(&lock);
            if (notify(SUMI_PHONE_NOTE_PCM, turn, offset, pcm + offset, length)) {
                offset += length;
                continue;
            }
            portENTER_CRITICAL(&lock);
            sent = offset;
            portEXIT_CRITICAL(&lock);
        }
        if (esp_timer_get_time() >= deadline) return false;
        vTaskDelay(1);
    }
    return false;
}

const char *sumi_phone_exchange(uint32_t turn, const int16_t *pcm, size_t frames,
                                bool (*interrupted)(void))
{
    if (!frames || frames > SUMI_PHONE_NOTE_MAX / sizeof(*pcm)) return "録音が空か長すぎます";
    uint8_t *buffer = heap_caps_malloc(SUMI_PHONE_PLAYBACK_CAPACITY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) return "再生に必要なメモリーが不足しています";
    portENTER_CRITICAL(&lock);
    reply.bytes = buffer;
    portEXIT_CRITICAL(&lock);
    muse_state_set_mode(MUSE_MODE_THINKING);
    muse_state_set_caption("スマホへ録音を送信中");
    int64_t upload_start = esp_timer_get_time();
    if (!send_note(turn, (const uint8_t *)pcm, frames * sizeof(*pcm), interrupted))
        return "スマホへ録音を送信できません";
    ESP_LOGI("sumi_phone", "note transfer: %u bytes in %lld ms",
             (unsigned)(frames * sizeof(*pcm)), (esp_timer_get_time() - upload_start) / 1000);
    muse_state_set_caption("スマホの返答を待っています");

    bool started = false;
    unsigned underruns = 0;
    uint32_t written = 0;
    int64_t begin = esp_timer_get_time();
    int64_t deadline = begin + REPLY_TIMEOUT_US;
    int64_t hard_deadline = begin + STREAM_TOTAL_US;
    while (active(turn, interrupted)) {
        int16_t samples[MUSE_AUDIO_CHUNK];
        char next_caption[sizeof(caption)] = {0};
        portENTER_CRITICAL(&lock);
        if (caption_pending) {
            memcpy(next_caption, caption, sizeof(caption));
            caption_pending = false;
        }
        size_t available = reply.written - reply.consumed;
        bool finished = reply.finished;
        bool streaming = reply.streaming;
        int64_t wait_until = generation_wait_until;
        uint32_t received = reply.written;
        bool can_play = available && (finished || (started ? available >= PCM_BYTES : available >= SUMI_PHONE_PREROLL));
        size_t length = can_play ? sumi_phone_buffer_read(&reply, samples, sizeof(samples)) : 0;
        portEXIT_CRITICAL(&lock);
        if (next_caption[0]) muse_state_set_caption("%s", next_caption);
        if (received != written) {
            written = received;
            deadline = esp_timer_get_time() + (streaming ? STREAM_WAIT_US : WINDOW_TIMEOUT_US);
        }
        if (length) {
            if (!active(turn, interrupted)) break;
            if (!started) {
                started = true;
                muse_state_set_mode(MUSE_MODE_SPEAKING);
                portENTER_CRITICAL(&lock);
                bool has_caption = caption[0] != '\0';
                portEXIT_CRITICAL(&lock);
                if (!has_caption) muse_state_set_caption("スマホからの返答");
                ESP_LOGI("sumi_phone", "reply audio start after %lld ms", (esp_timer_get_time() - begin) / 1000);
            }
            muse_state_set_level(muse_audio_level(samples, length / sizeof(*samples)));
            if (muse_audio_write(samples, length / sizeof(*samples)) != ESP_OK) return "返答を再生できません";
        } else if (finished) {
            ESP_LOGI("sumi_phone", "reply finished: %lu bytes, %u empty playback blocks", (unsigned long)written, underruns);
            return NULL;
        } else if (started && !streaming) {
            memset(samples, 0, sizeof(samples));
            ++underruns;
            if (muse_audio_write(samples, MUSE_AUDIO_CHUNK) != ESP_OK) return "返答を再生できません";
        } else {
            vTaskDelay(1);
        }
        int64_t now = esp_timer_get_time();
        if (now >= hard_deadline || (now >= deadline && now >= wait_until))
            return "スマホからの返答が途切れました";
    }
    return "スマホとの会話を中断しました";
}
