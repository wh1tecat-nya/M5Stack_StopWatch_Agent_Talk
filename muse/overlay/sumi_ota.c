#include "sumi_ota.h"
#include "sdkconfig.h"
#include "host/ble_hs.h"

#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "miniz.h"
#include "sumi_ui.h"

#include <stdatomic.h>
#include <string.h>

enum { OTA_INFO = 1, OTA_BEGIN, OTA_DATA, OTA_FINISH, OTA_ABORT, OTA_BEGIN_COMPRESSED, OTA_BEGIN_WINDOW, OTA_PROGRESS };
enum { OTA_BUSY = 0x80, OTA_FAILED, OTA_BAD_IMAGE, OTA_NO_MEMORY };
#define SLOT_BYTES 0x400000u
#define HEADER 6u
#define IDLE_MS 30000u
#define BLOCK_BYTES 8192u

// The BLE host owns the upload until FINISH hands it to the verification task.
static esp_ota_handle_t handle;
static bool open;
static bool window_mode;
static uint8_t window_error;
static uint8_t info_version;
static const esp_partition_t *target;
static uint32_t declared, written, received;
static atomic_uint deadline;
static atomic_bool finalizing, rebooting;
static atomic_int completion;

static struct compressed_upload {
    tinfl_decompressor inflater;
    uint8_t block[BLOCK_BYTES + 2];
    size_t used;
    uint8_t *decoded;
} *compressed;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

bool sumi_ota_busy(void)
{
    uint32_t expires = atomic_load(&deadline);
    return atomic_load(&finalizing) || atomic_load(&rebooting) ||
           (expires && (int32_t)(expires - now_ms()) > 0);
}

static void release_compressed(void)
{
    if (!compressed) return;
    free(compressed->decoded);
    free(compressed);
    compressed = NULL;
}

static void abort_upload(void)
{
    if (open) esp_ota_abort(handle);
    release_compressed();
    open = false;
    target = NULL;
    atomic_store(&deadline, 0);
}

void sumi_ota_disconnect(void)
{
    info_version = 0;
    window_mode = false;
    if (!atomic_load(&finalizing) && !atomic_load(&rebooting)) abort_upload();
}

static uint32_t packet_argument(const uint8_t *data)
{
    return data[2] | (uint32_t)data[3] << 8 | (uint32_t)data[4] << 16 | (uint32_t)data[5] << 24;
}

static bool receive_compressed(const uint8_t *data, size_t length)
{
    while (length) {
        if (written == declared) return false;
        size_t block_bytes = declared - written;
        if (block_bytes > BLOCK_BYTES) block_bytes = BLOCK_BYTES;
        if (compressed->used < 2) {
            size_t copy = 2 - compressed->used;
            if (copy > length) copy = length;
            memcpy(compressed->block + compressed->used, data, copy);
            compressed->used += copy;
            data += copy;
            length -= copy;
            if (compressed->used != 2) return true;
        }
        size_t encoded = compressed->block[0] | (size_t)compressed->block[1] << 8;
        if (!encoded || encoded > block_bytes) return false;
        size_t copy = encoded + 2 - compressed->used;
        if (copy > length) copy = length;
        memcpy(compressed->block + compressed->used, data, copy);
        compressed->used += copy;
        data += copy;
        length -= copy;
        if (compressed->used != encoded + 2) return true;

        const uint8_t *payload = compressed->block + 2;
        if (encoded < block_bytes) {
            tinfl_init(&compressed->inflater);
            size_t input = encoded, output = block_bytes;
            uint8_t *decoded = compressed->decoded;
            if (tinfl_decompress(&compressed->inflater, payload, &input, decoded, decoded, &output,
                    TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF) != TINFL_STATUS_DONE ||
                input != encoded || output != block_bytes) return false;
            payload = decoded;
        } else {
            memcpy(compressed->decoded, payload, block_bytes);
            payload = compressed->decoded;
        }
        if (esp_ota_write(handle, payload, block_bytes) != ESP_OK) return false;
        written += block_bytes;
        compressed->used = 0;
    }
    return true;
}

static const esp_partition_t *update_slot(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next = running ? esp_ota_get_next_update_partition(running) : NULL;
    if (!running || !next || next->size != SLOT_BYTES || running->size != SLOT_BYTES ||
        !((running->address == 0x20000 && next->address == 0x420000) ||
          (running->address == 0x420000 && next->address == 0x20000))) return NULL;
    return next;
}

static bool running_valid(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    return running && esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_VALID;
}

static void finalize_task(void *unused)
{
    (void)unused;
    ESP_LOGI("sumi_ota", "Verifying %lu bytes in %s", (unsigned long)written, target->label);
    esp_err_t result = esp_ota_end(handle);
    open = false;
    if (result == ESP_OK) {
        result = esp_ota_set_boot_partition(target);
        if (result == ESP_OK) {
            atomic_store(&rebooting, true);
            ESP_LOGI("sumi_ota", "Verified update ready: %s", target->label);
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_restart();
        }
    }
    ESP_LOGW("sumi_ota", "Image verification or boot selection failed: %s", esp_err_to_name(result));
    abort_upload();
    atomic_store(&completion, OTA_BAD_IMAGE);
    atomic_store(&finalizing, false);
    vTaskDelete(NULL);
}

int sumi_ota_command(const uint8_t *data, size_t size, bool idle)
{
    if (size < HEADER || size > SUMI_OTA_PACKET_MAX || data[0] != SUMI_OTA_TAG)
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    uint32_t arg = packet_argument(data);
    size_t payload = size - HEADER;
    if (data[1] == OTA_INFO && arg <= 2 && !payload && update_slot()) {
        info_version = 2 + arg;
        return 0;
    }
    if (atomic_load(&finalizing) || atomic_load(&rebooting)) return OTA_BUSY;
    if (open && !sumi_ota_busy()) abort_upload();
    switch (data[1]) {
    case OTA_BEGIN:
    case OTA_BEGIN_COMPRESSED:
    case OTA_BEGIN_WINDOW:
        if (payload || arg < 8192 || arg > SLOT_BYTES) break;
        if (!idle || open || !sumi_ui_ready() || !running_valid()) return OTA_BUSY;
        target = update_slot();
        if (!target) return OTA_FAILED;
        if (data[1] != OTA_BEGIN) {
            compressed = heap_caps_calloc(1, sizeof(*compressed), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (compressed) compressed->decoded = heap_caps_malloc_prefer(BLOCK_BYTES, 2,
                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (!compressed || !compressed->decoded) {
                abort_upload();
                return OTA_NO_MEMORY;
            }
        }
        atomic_store(&deadline, now_ms() + IDLE_MS);
        if (esp_ota_begin(target, OTA_WITH_SEQUENTIAL_WRITES, &handle) != ESP_OK) {
            abort_upload();
            return OTA_FAILED;
        }
        open = true;
        window_mode = data[1] == OTA_BEGIN_WINDOW;
        window_error = 0;
        declared = arg;
        received = written = 0;
        atomic_store(&completion, 0);
        return 0;
    case OTA_DATA:
        if (!open || arg != received || !payload) break;
        if (compressed) {
            uint32_t limit = declared + 2 * ((declared + BLOCK_BYTES - 1) / BLOCK_BYTES);
            if (payload > limit - received || !receive_compressed(data + HEADER, payload)) {
                abort_upload();
                return OTA_BAD_IMAGE;
            }
        } else {
            if (payload > declared - written) break;
            if (esp_ota_write(handle, data + HEADER, payload) != ESP_OK) {
                abort_upload();
                return OTA_FAILED;
            }
            written += payload;
        }
        received += payload;
        atomic_store(&deadline, now_ms() + IDLE_MS);
        return 0;
    case OTA_FINISH:
        if (!open || window_error || payload || arg != declared || written != declared) break;
        window_mode = false;
        release_compressed();
        atomic_store(&finalizing, true);
        if (xTaskCreate(finalize_task, "sumi_ota", 16384, NULL, 5, NULL) != pdPASS) {
            atomic_store(&finalizing, false);
            abort_upload();
            return OTA_FAILED;
        }
        return 0;
    case OTA_ABORT:
        if (payload || arg) break;
        abort_upload();
        window_mode = false;
        window_error = 0;
        return 0;
    }
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
}

int sumi_ota_window_data(const uint8_t *data, size_t size)
{
    if (!window_mode) return BLE_ATT_ERR_REQ_NOT_SUPPORTED;
    if (window_error) return window_error;
    if (size <= HEADER || size > SUMI_OTA_PACKET_MAX || data[0] != SUMI_OTA_TAG || data[1] != OTA_DATA)
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    if (!open || !sumi_ota_busy()) {
        abort_upload();
        return window_error = OTA_FAILED;
    }
    uint32_t offset = packet_argument(data);
    // A read may run before the last write reaches the peer. Accept a replay
    // only when the whole packet is already behind the receive cursor.
    if (offset < received && size - HEADER <= received - offset) return 0;
    if (offset != received) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    return window_error = sumi_ota_command(data, size, false);
}

size_t sumi_ota_window_progress(uint8_t out[SUMI_OTA_PROGRESS_SIZE])
{
    if (!window_mode) return 0;
    if (open && !sumi_ota_busy()) {
        abort_upload();
        window_error = OTA_FAILED;
    }
    out[0] = SUMI_OTA_TAG;
    out[1] = OTA_PROGRESS;
    for (unsigned i = 0; i < 4; ++i) out[2 + i] = received >> (8 * i);
    out[6] = window_error;
    return SUMI_OTA_PROGRESS_SIZE;
}

size_t sumi_ota_status(uint8_t out[16])
{
    if (!info_version) return 0;
    uint8_t version = info_version;
    info_version = 0;
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (!running) return 0;
    out[0] = version;
    out[1] = running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1;
    out[2] = running_valid();
    memcpy(out + 3, esp_app_get_description()->app_elf_sha256, 12);
    out[15] = atomic_load(&completion);
    return 16;
}
#else
bool sumi_ota_busy(void) { return false; }
void sumi_ota_disconnect(void) {}
int sumi_ota_command(const uint8_t *data, size_t size, bool idle)
{
    (void)data;
    (void)size;
    (void)idle;
    return BLE_ATT_ERR_REQ_NOT_SUPPORTED;
}
size_t sumi_ota_status(uint8_t out[16]) { (void)out; return 0; }
int sumi_ota_window_data(const uint8_t *data, size_t size)
{
    (void)data;
    (void)size;
    return BLE_ATT_ERR_REQ_NOT_SUPPORTED;
}
size_t sumi_ota_window_progress(uint8_t out[SUMI_OTA_PROGRESS_SIZE]) { (void)out; return 0; }
#endif
