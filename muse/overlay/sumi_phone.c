#include "sumi_phone.h"
#include "sumi_phone_audio.h"

#include "muse_audio.h"
#include "muse_ble.h"
#include "muse_state.h"
#include "muse_voice.h"
#include "muse_wifi.h"
#include "sumi_chat.h"
#include "sumi_ota.h"
#include "sumi_ui.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "mbedtls/platform_util.h"
#include "nvs.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WINDOW_TIMEOUT_US (5 * 1000000LL)
#define REPLY_TIMEOUT_US (90 * 1000000LL)
#define STREAM_WAIT_US (40 * 1000000LL)
#define STREAM_TOTAL_US (180 * 1000000LL)
#define PCM_BYTES (MUSE_AUDIO_CHUNK * sizeof(int16_t))
#define SETTINGS_DURATION_MS (5 * 60 * 1000)
#define MUSE_ACCESS_TAG 0xa5
#define CHAT_CATALOG_TAG 0xa6

enum { MUSE_ACCESS_READ = 1, MUSE_ACCESS_BEGIN_WRITE, MUSE_ACCESS_WRITE, MUSE_ACCESS_COMMIT, MUSE_ACCESS_CLEAR,
       MUSE_PAIR_READ, MUSE_PAIR_BEGIN, MUSE_PAIR_RESERVE, MUSE_PAIR_RELEASE };
enum { MUSE_ACCESS_WINDOW = 0x80, MUSE_ACCESS_BUSY, MUSE_ACCESS_MISSING, MUSE_ACCESS_CHANGED, MUSE_ACCESS_SAVE_FAILED };
#define MUSE_ACCESS_CHUNK 128
#define MUSE_PAIR_PAYLOAD_MAX (14 + 31 + 2 * SUMI_MUSE_ACCESS_MAX)
#define MUSE_PAIR_REV_BYTES 8

static atomic_bool phone_mode;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static uint16_t conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool subscribed;
static bool hello;
static bool note_stream_enabled;
static uint32_t next_turn;
static uint32_t queued_push;
static uint32_t queued_catalog;
static uint8_t queued_selection;
static int64_t queued_until;
static uint32_t sent;
static uint32_t acked;
static bool note_finished;
static bool phone_error;
static bool ota_admitting;
static int64_t generation_wait_until;
static char caption[236];
static bool caption_pending;
static struct sumi_phone_buffer reply;
/* The voice task advances this upload. The BLE host updates ACKs under lock. */
struct note_upload {
    char session[SUMI_CHAT_ID_BYTES + 1];
    unsigned scope_sent;
    bool streaming;
    bool begun;
};
static struct note_upload note;
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
static bool chat_status_pending;
static uint8_t *chat_upload;
static size_t chat_length, chat_received;
static atomic_uint muse_sync_until;
// Accessed only by the NimBLE host task.
static struct {
    size_t length;
    size_t offset;
    bool reading;
    bool read_pending;
    bool pair;
    uint8_t *bytes;
} muse_access;

static void clear_muse_access(void)
{
    if (muse_access.bytes) mbedtls_platform_zeroize(muse_access.bytes, MUSE_PAIR_PAYLOAD_MAX + 1);
    free(muse_access.bytes);
    memset(&muse_access, 0, sizeof(muse_access));
}

static bool start_muse_access(bool pair)
{
    clear_muse_access();
    muse_access.pair = pair;
    muse_access.bytes = heap_caps_calloc(1, MUSE_PAIR_PAYLOAD_MAX + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return muse_access.bytes != NULL;
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

static void clear_chat_upload(void)
{
    free(chat_upload);
    chat_upload = NULL;
    chat_length = chat_received = 0;
}

bool sumi_phone_settings_enabled(void)
{
    return atomic_load(&settings_enabled) || app_sumi_muse_refresh_pending();
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
    if (sumi_ota_busy()) return false;
    if (!enabled && app_sumi_muse_refresh_pending()) sumi_phone_open_settings();
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READWRITE, &nvs) != ESP_OK) return false;
    esp_err_t result = nvs_set_i32(nvs, "phone_mode", enabled);
    if (result == ESP_OK) result = nvs_commit(nvs);
    nvs_close(nvs);
    if (result != ESP_OK) return false;
    portENTER_CRITICAL(&lock);
    atomic_store(&phone_mode, enabled);
    hello = false;
    queued_push = 0;
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

static void begin_locked(uint32_t turn)
{
    reply = (struct sumi_phone_buffer){.turn = turn};
    note = (struct note_upload){.streaming = note_stream_enabled};
    sent = acked = 0;
    note_finished = phone_error = false;
    generation_wait_until = 0;
    caption_pending = false;
    caption[0] = '\0';
}

uint32_t sumi_phone_begin(void)
{
    uint32_t seed = esp_random();
    portENTER_CRITICAL(&lock);
    uint32_t turn = 0;
    if (ready_locked() && !reply.turn && !setting_pending && !ota_admitting && !sumi_ota_busy()) {
        if (!next_turn) next_turn = seed;
        if (++next_turn == 0) ++next_turn;
        turn = next_turn;
        queued_push = 0;
        begin_locked(turn);
    }
    portEXIT_CRITICAL(&lock);
    return turn;
}

uint32_t sumi_phone_take_push(void)
{
    portENTER_CRITICAL(&lock);
    uint32_t turn = queued_push;
    uint32_t catalog = queued_catalog;
    uint8_t selection = queued_selection;
    queued_push = 0;
    if (turn && ready_locked() && !reply.turn && !setting_pending && !ota_admitting && !sumi_ota_busy() &&
        esp_timer_get_time() < queued_until)
        begin_locked(turn);
    else turn = 0;
    portEXIT_CRITICAL(&lock);
    if (turn && (sumi_chat_catalog_hash() != catalog || sumi_chat_selected() != selection)) {
        sumi_phone_end(turn, true);
        return 0;
    }
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
    if (event->type == BLE_GAP_EVENT_CONNECT || event->type == BLE_GAP_EVENT_DISCONNECT ||
        (event->type == BLE_GAP_EVENT_ENC_CHANGE && event->enc_change.status != 0)) {
        clear_muse_access();
        sumi_ota_disconnect();
        chat_status_pending = false;
        clear_chat_upload();
    }
    portENTER_CRITICAL(&lock);
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            conn_handle = event->connect.conn_handle;
            subscribed = hello = false;
            queued_push = 0;
            if (reply.turn) phone_error = true;
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        if (event->disconnect.conn.conn_handle == conn_handle) {
            conn_handle = BLE_HS_CONN_HANDLE_NONE;
            setting_conn = BLE_HS_CONN_HANDLE_NONE;
            subscribed = hello = false;
            queued_push = 0;
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

static bool valid_access(const uint8_t *bytes, size_t length)
{
    if (length < 10 || length > SUMI_MUSE_ACCESS_MAX ||
        !memcmp(bytes, "mgst_", 5) || (length >= 14 && !memcmp(bytes, "hatch_refresh:", 14)))
        return false;
    for (size_t i = 0; i < length; ++i)
        if (bytes[i] < '!' || bytes[i] > '~') return false;
    return true;
}

static bool muse_peer(uint16_t conn, char peer[16])
{
    struct ble_gap_conn_desc desc;
    if (ble_gap_conn_find(conn, &desc) != 0) return false;
    const uint8_t *addr = desc.peer_id_addr.val;
    snprintf(peer, 16, "%02x%02x%02x%02x%02x%02x%02x", desc.peer_id_addr.type,
             addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]);
    return true;
}

static int muse_store_status(int result)
{
    switch (result) {
    case 1: return 0;
    case 0: return MUSE_ACCESS_CHANGED;
    case -1: return MUSE_ACCESS_BUSY;
    default: return MUSE_ACCESS_SAVE_FAILED;
    }
}

static int muse_access_command(uint16_t conn, struct ble_gatt_access_ctxt *ctxt)
{
    unsigned until = atomic_load(&muse_sync_until);
    if (!secure_connection(conn)) return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    bool window = until && (int32_t)(until - xTaskGetTickCount()) > 0;
    portENTER_CRITICAL(&lock);
    bool busy = conn != conn_handle || setting_pending;
    bool speaking = reply.turn != 0;
    portEXIT_CRITICAL(&lock);
    if (busy) return MUSE_ACCESS_BUSY;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        if (!muse_access.pair && !window) { clear_muse_access(); return MUSE_ACCESS_WINDOW; }
        if (!muse_access.reading || muse_access.offset >= muse_access.length) return BLE_ATT_ERR_UNLIKELY;
        uint8_t header[2];
        store16(header, muse_access.length);
        size_t length = muse_access.length - muse_access.offset;
        // Leave one byte unused so Android completes this read without a Read Blob request.
        size_t capacity = ble_att_mtu(conn) - 4;
        if (capacity > MUSE_ACCESS_CHUNK) capacity = MUSE_ACCESS_CHUNK;
        if (length > capacity) length = capacity;
        return os_mbuf_append(ctxt->om, header, sizeof(header)) ||
               os_mbuf_append(ctxt->om, muse_access.bytes + muse_access.offset, length)
            ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
    }
    uint8_t frame[4 + MUSE_ACCESS_CHUNK];
    uint16_t length;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR ||
        ble_hs_mbuf_to_flat(ctxt->om, frame, sizeof(frame), &length) || length < 4)
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    uint16_t value = read16(frame + 2);
    uint8_t command = frame[1];
    bool pair = command == MUSE_PAIR_READ || command == MUSE_PAIR_BEGIN ||
                command == MUSE_PAIR_RESERVE || command == MUSE_PAIR_RELEASE ||
                (muse_access.pair && command >= MUSE_ACCESS_WRITE && command <= MUSE_ACCESS_CLEAR);
    if (!pair && !window) { clear_muse_access(); return MUSE_ACCESS_WINDOW; }
    if (!pair && speaking) return MUSE_ACCESS_BUSY;
    char peer[16];
    if (pair && !muse_peer(conn, peer)) return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    if (length == 4 + MUSE_PAIR_REV_BYTES && value == 0 &&
        (command == MUSE_PAIR_RESERVE || command == MUSE_PAIR_RELEASE))
        return muse_store_status(app_sumi_muse_reserve(peer, frame + 4, command == MUSE_PAIR_RESERVE));
    int result = BLE_ATT_ERR_UNLIKELY;
    if (length == 4) {
        switch (command) {
        case MUSE_ACCESS_READ:
        case MUSE_PAIR_READ:
            if (!value) {
                if (!start_muse_access(pair)) return BLE_ATT_ERR_INSUFFICIENT_RES;
                int size = pair ? app_sumi_muse_read_pair(peer, window, muse_access.bytes, MUSE_PAIR_PAYLOAD_MAX)
                                : app_sumi_muse_read_access((char *)muse_access.bytes, SUMI_MUSE_ACCESS_MAX + 1);
                if (size <= 0 || (!pair && !valid_access(muse_access.bytes, size))) {
                    clear_muse_access();
                    if (size == -1) return MUSE_ACCESS_BUSY;
                    if (size == -3) return MUSE_ACCESS_WINDOW;
                    return MUSE_ACCESS_MISSING;
                }
                muse_access.length = size;
                muse_access.reading = true;
            }
            if (!muse_access.reading || muse_access.pair != pair || value >= muse_access.length) break;
            muse_access.offset = value;
            muse_access.read_pending = true;
            return 0;
        case MUSE_ACCESS_BEGIN_WRITE:
            clear_muse_access();
            if (value < 10 || value > SUMI_MUSE_ACCESS_MAX) break;
            if (!start_muse_access(false)) return BLE_ATT_ERR_INSUFFICIENT_RES;
            muse_access.length = SUMI_MUSE_ACCESS_DIGEST + value;
            return 0;
        case MUSE_PAIR_BEGIN:
            if (value < 14 || value > 12 + 2 * SUMI_MUSE_ACCESS_MAX) break;
            if (!app_sumi_muse_refresh_pending()) return MUSE_ACCESS_CHANGED;
            if (!start_muse_access(true)) return BLE_ATT_ERR_INSUFFICIENT_RES;
            muse_access.length = value;
            return 0;
        case MUSE_ACCESS_COMMIT:
            if (!value && !muse_access.reading && muse_access.length && muse_access.offset == muse_access.length) {
                if (pair) {
                    result = muse_store_status(app_sumi_muse_commit(peer, muse_access.bytes, muse_access.length));
                } else if (valid_access(muse_access.bytes + SUMI_MUSE_ACCESS_DIGEST, muse_access.length - SUMI_MUSE_ACCESS_DIGEST)) {
                    result = muse_store_status(app_sumi_muse_replace_access(
                            (char *)muse_access.bytes + SUMI_MUSE_ACCESS_DIGEST, muse_access.bytes));
                }
            }
            clear_muse_access();
            return result;
        case MUSE_ACCESS_CLEAR:
            clear_muse_access();
            return 0;
        }
    } else if (command == MUSE_ACCESS_WRITE && !muse_access.reading && value == muse_access.offset &&
               value + length - 4 <= muse_access.length) {
        memcpy(muse_access.bytes + value, frame + 4, length - 4);
        muse_access.offset += length - 4;
        result = 0;
    }
    mbedtls_platform_zeroize(frame, sizeof(frame));
    return result;
}

static bool accept_locked(const struct sumi_phone_packet *packet)
{
    if (packet->type == SUMI_PHONE_HELLO) {
        if (!subscribed || reply.turn || packet->turn || packet->length || packet->offset != SUMI_PHONE_VERSION) return false;
        hello = true;
        note_stream_enabled = false;
        return true;
    }
    if (packet->type == SUMI_PHONE_NOTE_STREAM_ENABLE) {
        if (!ready_locked() || packet->turn || packet->offset || packet->length) return false;
        note_stream_enabled = true;
        return true;
    }
    if (packet->type == SUMI_PHONE_PUSH_BEGIN) {
        int64_t now = esp_timer_get_time();
        if (!ready_locked() || reply.turn || setting_pending || ota_admitting || sumi_ota_busy() ||
            (queued_push && now < queued_until) ||
            !packet->turn || packet->length != 1 || packet->data[0] > SUMI_CHAT_MAX) return false;
        queued_push = packet->turn;
        queued_catalog = packet->offset;
        queued_selection = packet->data[0];
        queued_until = now + WINDOW_TIMEOUT_US;
        return true;
    }
    if (!ready_locked() || !reply.turn || packet->turn != reply.turn || phone_error) return false;
    switch (packet->type) {
    case SUMI_PHONE_NOTE_ACK:
        if (packet->length || packet->offset < acked || packet->offset > sent || (packet->offset & 1)) return false;
        acked = packet->offset;
        return true;
    case SUMI_PHONE_REPLY_BEGIN:
        return note_finished && sumi_phone_buffer_begin(&reply, packet);
    case SUMI_PHONE_REPLY_PLAY:
        return sumi_phone_buffer_allow_playback(&reply, packet);
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
                                 reply.turn ? reply.written : SUMI_PHONE_FEATURE_STREAM | SUMI_PHONE_FEATURE_PUSH |
                                 SUMI_PHONE_FEATURE_NOTE_STREAM | SUMI_PHONE_FEATURE_REPLY_PREFILL);
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
    if (accepted && packet.type == SUMI_PHONE_PUSH_BEGIN) muse_state_nudge();
    return accepted ? 0 : BLE_ATT_ERR_UNLIKELY;
}

static int chat_catalog_command(uint16_t conn, struct os_mbuf *packet)
{
    uint8_t frame[4 + MUSE_ACCESS_CHUNK];
    uint16_t length;
    if (ble_hs_mbuf_to_flat(packet, frame, sizeof(frame), &length) || length < 4)
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    portENTER_CRITICAL(&lock);
    bool active = conn_handle == conn;
    bool busy = reply.turn || setting_pending;
    portEXIT_CRITICAL(&lock);
    if (!active) return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    unsigned value = read16(frame + 2);
    if (frame[1] == 1 && length == 4 && !value) {
        chat_status_pending = true;
        return 0;
    }
    switch (frame[1]) {
    case 2:
        clear_chat_upload();
        if (length != 4 || value < 2 || value > SUMI_CHAT_CATALOG_MAX) break;
        chat_upload = heap_caps_malloc(value, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!chat_upload) return BLE_ATT_ERR_INSUFFICIENT_RES;
        chat_length = value;
        return 0;
    case 3:
        if (!chat_upload || value != chat_received || length == 4 ||
            (size_t)(length - 4) > chat_length - chat_received) break;
        memcpy(chat_upload + chat_received, frame + 4, length - 4);
        chat_received += length - 4;
        return 0;
    case 4:
        if (length != 4 || value || !chat_upload || chat_received != chat_length) break;
        if (busy || muse_voice_in_call() || muse_voice_notes_waiting()) return MUSE_ACCESS_BUSY;
        muse_mode_t mode = muse_state_mode(NULL);
        if (mode == MUSE_MODE_LISTENING || mode == MUSE_MODE_THINKING || mode == MUSE_MODE_SPEAKING)
            return MUSE_ACCESS_BUSY;
        bool saved = sumi_chat_sync(chat_upload, chat_length);
        clear_chat_upload();
        return saved ? 0 : MUSE_ACCESS_SAVE_FAILED;
    }
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
}

int sumi_phone_settings_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)attr;
    (void)arg;
    if ((!sumi_phone_mode() && !sumi_phone_settings_enabled()) || !secure_connection(conn))
        return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint8_t tag;
        if (!os_mbuf_copydata(ctxt->om, 0, 1, &tag)) {
            if (tag == SUMI_OTA_TAG) {
                if (!sumi_phone_mode()) return BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
                uint8_t data[SUMI_OTA_PACKET_MAX];
                uint16_t size;
                if (ble_hs_mbuf_to_flat(ctxt->om, data, sizeof(data), &size)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
                portENTER_CRITICAL(&lock);
                bool owner = conn_handle == conn && ready_locked();
                bool idle = owner && !reply.turn && !setting_pending && !queued_push;
                ota_admitting = idle;
                portEXIT_CRITICAL(&lock);
                int result = owner ? sumi_ota_command(data, size, idle) : BLE_ATT_ERR_INSUFFICIENT_AUTHOR;
                portENTER_CRITICAL(&lock);
                ota_admitting = false;
                portEXIT_CRITICAL(&lock);
                return result;
            }
            if (sumi_ota_busy()) return BLE_ATT_ERR_INSUFFICIENT_RES;
            if (tag == MUSE_ACCESS_TAG) return muse_access_command(conn, ctxt);
            if (tag == CHAT_CATALOG_TAG) return chat_catalog_command(conn, ctxt->om);
        }
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        uint8_t ota_status[16];
        size_t ota_size = sumi_ota_status(ota_status);
        if (ota_size) return os_mbuf_append(ctxt->om, ota_status, ota_size) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
        if (chat_status_pending) {
            chat_status_pending = false;
            uint32_t hash = sumi_chat_catalog_hash();
            uint8_t bytes[5] = {hash, hash >> 8, hash >> 16, hash >> 24, sumi_chat_selected()};
            return os_mbuf_append(ctxt->om, bytes, sizeof(bytes)) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
        }
        if (muse_access.read_pending) {
            muse_access.read_pending = false;
            return muse_access_command(conn, ctxt);
        }
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
    settings_until = xTaskGetTickCount() + pdMS_TO_TICKS(SETTINGS_DURATION_MS);
    atomic_store(&settings_enabled, true);
    muse_ble_apply();
}

void sumi_phone_allow_muse_sync(void)
{
    atomic_store(&muse_sync_until, xTaskGetTickCount() + pdMS_TO_TICKS(SETTINGS_DURATION_MS));
    sumi_phone_open_settings();
}

void sumi_phone_apply_setting(void)
{
    unsigned until = atomic_load(&muse_sync_until);
    if (until && (int32_t)(until - xTaskGetTickCount()) <= 0)
        atomic_compare_exchange_strong(&muse_sync_until, &until, 0);
    if (atomic_load(&settings_enabled) && (int32_t)(xTaskGetTickCount() - settings_until) >= 0) {
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
    if (pending && !disconnected && !busy && !sumi_ota_busy()) {
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

static bool send_note_header(uint32_t turn, uint32_t bytes)
{
    if (!note.begun) {
        sumi_chat_id(note.session);
        uint8_t options[] = {!note.session[0] ? 0 : strcmp(note.session, SUMI_CHAT_LEGACY_ID) == 0 ? 1 : 2,
                             sumi_chat_wearable()};
        uint32_t length = note.streaming ? SUMI_PHONE_NOTE_STREAM : bytes;
        if (!notify(SUMI_PHONE_NOTE_BEGIN, turn, length, options, sizeof(options))) return false;
        note.begun = true;
        if (options[0] != 2) note.scope_sent = SUMI_CHAT_ID_BYTES;
    }
    while (note.scope_sent < SUMI_CHAT_ID_BYTES) {
        portENTER_CRITICAL(&lock);
        uint16_t conn = conn_handle;
        portEXIT_CRITICAL(&lock);
        unsigned mtu = ble_att_mtu(conn);
        if (mtu <= 3 + SUMI_PHONE_HEADER) return false;
        unsigned count = mtu - 3 - SUMI_PHONE_HEADER;
        unsigned offset = note.scope_sent;
        if (count > SUMI_CHAT_ID_BYTES - offset) count = SUMI_CHAT_ID_BYTES - offset;
        if (!notify(SUMI_PHONE_NOTE_SCOPE, turn, offset, note.session + offset, count)) return false;
        note.scope_sent += count;
    }
    return true;
}

static bool send_note_pcm(uint32_t turn, const uint8_t *pcm, uint32_t bytes, bool last)
{
    portENTER_CRITICAL(&lock);
    uint16_t conn = conn_handle;
    uint32_t offset = sent;
    uint32_t in_flight = sent - acked;
    portEXIT_CRITICAL(&lock);
    unsigned mtu = ble_att_mtu(conn);
    if (mtu < 3 + SUMI_PHONE_HEADER + 2 || offset >= bytes || in_flight >= SUMI_PHONE_UPLOAD_WINDOW) return false;
    size_t capacity = ((mtu - 3 < SUMI_PHONE_PACKET_MAX ? mtu - 3 : SUMI_PHONE_PACKET_MAX) - SUMI_PHONE_HEADER) & ~1u;
    size_t length = bytes - offset;
    if (length > capacity) length = capacity;
    size_t credit = SUMI_PHONE_UPLOAD_WINDOW - in_flight;
    if (length > credit) length = credit;
    if (!last && length < capacity) return false;
    portENTER_CRITICAL(&lock);
    sent = offset + length;
    portEXIT_CRITICAL(&lock);
    if (notify(SUMI_PHONE_NOTE_PCM, turn, offset, pcm + offset, length)) return true;
    portENTER_CRITICAL(&lock);
    sent = offset;
    portEXIT_CRITICAL(&lock);
    return false;
}

void sumi_phone_note_progress(uint32_t turn, const int16_t *pcm, size_t frames)
{
    if (!note.streaming || !sumi_phone_current(turn) || !send_note_header(turn, 0)) return;
    // Yield to the next capture read even when the link has a backlog.
    for (unsigned i = 0; i < 8; ++i) {
        if (!send_note_pcm(turn, (const uint8_t *)pcm, frames * sizeof(*pcm), false)) break;
    }
}

static bool send_note(uint32_t turn, const uint8_t *pcm, uint32_t bytes, bool (*interrupted)(void))
{
    int64_t deadline = esp_timer_get_time() + WINDOW_TIMEOUT_US;
    while (true) {
        if (!active(turn, interrupted)) return false;
        if (send_note_header(turn, bytes)) break;
        if (esp_timer_get_time() >= deadline) return false;
        vTaskDelay(1);
    }
    uint32_t previous_ack = 0;
    while (active(turn, interrupted)) {
        portENTER_CRITICAL(&lock);
        uint32_t acknowledged = acked;
        bool complete = (note.streaming ? sent : acknowledged) == bytes;
        if (complete) note_finished = true;
        portEXIT_CRITICAL(&lock);
        if (acknowledged != previous_ack) {
            previous_ack = acknowledged;
            deadline = esp_timer_get_time() + WINDOW_TIMEOUT_US;
        }
        if (complete) {
            // In streaming mode NOTE_END supplies the length after the final PCM notification.
            if (notify(SUMI_PHONE_NOTE_END, turn, bytes, NULL, 0)) return true;
        } else if (send_note_pcm(turn, pcm, bytes, true)) continue;
        if (esp_timer_get_time() >= deadline) return false;
        vTaskDelay(1);
    }
    return false;
}

static const char *exchange(uint32_t turn, const int16_t *pcm, size_t frames,
                            bool (*interrupted)(void))
{
    uint8_t *buffer = heap_caps_malloc(SUMI_PHONE_PLAYBACK_CAPACITY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) return "再生に必要なメモリーが不足しています";
    portENTER_CRITICAL(&lock);
    reply.bytes = buffer;
    uint32_t uploaded = sent;
    if (!pcm) note_finished = true;
    portEXIT_CRITICAL(&lock);
    muse_state_set_mode(MUSE_MODE_THINKING);
    if (pcm) {
        muse_state_set_caption("スマホへ録音を送信中");
        int64_t upload_start = esp_timer_get_time();
        if (!send_note(turn, (const uint8_t *)pcm, frames * sizeof(*pcm), interrupted))
            return "スマホへ録音を送信できません";
        ESP_LOGI("sumi_phone", "note transfer: %u bytes in %lld ms after recording, %lu bytes queued during recording",
                 (unsigned)(frames * sizeof(*pcm)), (esp_timer_get_time() - upload_start) / 1000, (unsigned long)uploaded);
        muse_state_set_caption("スマホの返答を待っています");
    } else {
        muse_state_set_caption("Museの返信を受信しました");
        int64_t until = esp_timer_get_time() + 1000000;
        while (!notify(SUMI_PHONE_PUSH_READY, turn, 0, NULL, 0)) {
            if (esp_timer_get_time() >= until || !active(turn, interrupted))
                return "スマホへ再生開始を通知できません";
            vTaskDelay(1);
        }
    }

    bool started = false;
    unsigned underruns = 0, stream_waits = 0;
    int64_t stream_wait_start = 0, stream_wait_total = 0;
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
        bool finished = reply.finished;
        bool streaming = reply.streaming;
        int64_t wait_until = generation_wait_until;
        uint32_t received = reply.written;
        bool can_play = sumi_phone_buffer_playable(&reply, started, PCM_BYTES);
        size_t length = can_play ? sumi_phone_buffer_read(&reply, samples, sizeof(samples)) : 0;
        portEXIT_CRITICAL(&lock);
        if (next_caption[0]) muse_state_set_caption("%s", next_caption);
        if (received != written) {
            written = received;
            deadline = esp_timer_get_time() + (streaming ? STREAM_WAIT_US : WINDOW_TIMEOUT_US);
        }
        if (length) {
            if (!active(turn, interrupted)) break;
            if (stream_wait_start) {
                ++stream_waits;
                stream_wait_total += esp_timer_get_time() - stream_wait_start;
                stream_wait_start = 0;
            }
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
            ESP_LOGI("sumi_phone", "reply finished: %lu bytes, %u empty playback blocks, %u stream buffer waits (%lld ms)",
                     (unsigned long)written, underruns, stream_waits, stream_wait_total / 1000);
            return NULL;
        } else if (started && !streaming) {
            memset(samples, 0, sizeof(samples));
            ++underruns;
            if (muse_audio_write(samples, MUSE_AUDIO_CHUNK) != ESP_OK) return "返答を再生できません";
        } else {
            if (started && !stream_wait_start) stream_wait_start = esp_timer_get_time();
            vTaskDelay(1);
        }
        int64_t now = esp_timer_get_time();
        if (now >= hard_deadline || (now >= deadline && now >= wait_until))
            return "スマホからの返答が途切れました";
    }
    return "スマホとの会話を中断しました";
}

const char *sumi_phone_exchange(uint32_t turn, const int16_t *pcm, size_t frames,
                                bool (*interrupted)(void))
{
    if (!frames || frames > SUMI_PHONE_NOTE_MAX / sizeof(*pcm)) return "録音が空か長すぎます";
    return exchange(turn, pcm, frames, interrupted);
}

const char *sumi_phone_play_push(uint32_t turn, bool (*interrupted)(void))
{
    const char *error = exchange(turn, NULL, 0, interrupted);
    if (!error) notify(SUMI_PHONE_PUSH_DONE, turn, 0, NULL, 0);
    sumi_phone_end(turn, error != NULL);
    return error;
}
