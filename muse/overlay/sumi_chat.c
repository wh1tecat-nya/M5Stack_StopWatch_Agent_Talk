#include "sumi_chat.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static SemaphoreHandle_t mutex;
static void lock_chats(void) { if (mutex) xSemaphoreTake(mutex, portMAX_DELAY); }
static void unlock_chats(void) { if (mutex) xSemaphoreGive(mutex); }
#else
static void lock_chats(void) {}
static void unlock_chats(void) {}
#endif

// Version, side-chat count, then UUID, UTF-8 byte count and title for each row.
static const uint8_t defaults[] = "\001\001" SUMI_CHAT_LEGACY_ID "\020M5Stack会話用";
static const uint8_t *catalog = defaults;
static size_t catalog_size = sizeof(defaults) - 1;
static int selected = 1;
static atomic_bool wearable = true;

bool sumi_chat_wearable(void) { return atomic_load(&wearable); }
void sumi_chat_set_wearable(bool enabled) { atomic_store(&wearable, enabled); }

static bool valid_id(const uint8_t *id)
{
    for (unsigned i = 0; i < SUMI_CHAT_ID_BYTES; ++i) {
        uint8_t c = id[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != '-') return false;
        } else if (!(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f')) return false;
    }
    return true;
}

static bool valid_catalog(const uint8_t *bytes, size_t size)
{
    if (size < 2 || size > SUMI_CHAT_CATALOG_MAX || bytes[0] != 1 || bytes[1] > SUMI_CHAT_MAX) return false;
    size_t offset = 2;
    const uint8_t *ids[SUMI_CHAT_MAX];
    for (unsigned i = 0; i < bytes[1]; ++i) {
        if (size - offset < SUMI_CHAT_ID_BYTES + 1 || !valid_id(bytes + offset)) return false;
        for (unsigned j = 0; j < i; ++j)
            if (memcmp(ids[j], bytes + offset, SUMI_CHAT_ID_BYTES) == 0) return false;
        ids[i] = bytes + offset;
        offset += SUMI_CHAT_ID_BYTES;
        size_t length = bytes[offset++];
        if (!length || length > SUMI_CHAT_TITLE_BYTES || size - offset < length) return false;
        for (unsigned j = 0; j < length; ++j)
            if (bytes[offset + j] < ' ' || bytes[offset + j] == 127) return false;
        offset += length;
    }
    return size == offset;
}

static const uint8_t *side_row(const uint8_t *bytes, int index)
{
    const uint8_t *row = bytes + 2;
    if (index < 1 || index > bytes[1]) return NULL;
    for (int i = 1; i < index; ++i) row += SUMI_CHAT_ID_BYTES + 1 + row[SUMI_CHAT_ID_BYTES];
    return row;
}

static bool save_catalog(const uint8_t *bytes, size_t size, int choice)
{
#ifdef ESP_PLATFORM
    uint8_t *record = malloc(size + 1);
    if (!record) return false;
    record[0] = choice;
    memcpy(record + 1, bytes, size);
    nvs_handle_t nvs;
    esp_err_t result = nvs_open("sumi-muse", NVS_READWRITE, &nvs);
    if (result == ESP_OK) {
        result = nvs_set_blob(nvs, "chat_catalog", record, size + 1);
        if (result == ESP_OK) result = nvs_commit(nvs);
        nvs_close(nvs);
    }
    free(record);
    return result == ESP_OK;
#else
    (void)bytes;
    (void)size;
    (void)choice;
    return true;
#endif
}

void sumi_chat_init(int legacy)
{
    selected = legacy == 0 ? 0 : 1;
#ifdef ESP_PLATFORM
    mutex = xSemaphoreCreateMutex();
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READONLY, &nvs) != ESP_OK) return;
    size_t size = 0;
    if (nvs_get_blob(nvs, "chat_catalog", NULL, &size) == ESP_OK && size >= 3 && size <= SUMI_CHAT_CATALOG_MAX + 1) {
        uint8_t *record = malloc(size);
        if (record && nvs_get_blob(nvs, "chat_catalog", record, &size) == ESP_OK &&
            valid_catalog(record + 1, size - 1) && record[0] <= record[2]) {
            selected = record[0];
            memmove(record, record + 1, size - 1);
            catalog = record;
            catalog_size = size - 1;
        } else free(record);
    }
    nvs_close(nvs);
#endif
}

bool sumi_chat_sync(const uint8_t *bytes, size_t size)
{
    if (!valid_catalog(bytes, size)) return false;
    lock_chats();
    if (size == catalog_size && memcmp(bytes, catalog, size) == 0) {
        unlock_chats();
        return true;
    }
    int choice = 0;
    const uint8_t *previous = side_row(catalog, selected);
    if (previous) for (int i = 1; i <= bytes[1]; ++i) {
        if (memcmp(side_row(bytes, i), previous, SUMI_CHAT_ID_BYTES) == 0) {
            choice = i;
            break;
        }
    }
    uint8_t *copy = malloc(size);
    bool saved = copy && save_catalog(bytes, size, choice);
    if (saved) {
        memcpy(copy, bytes, size);
        if (catalog != defaults) free((void *)catalog);
        catalog = copy;
        catalog_size = size;
        selected = choice;
    } else free(copy);
    unlock_chats();
    return saved;
}

uint32_t sumi_chat_catalog_hash(void)
{
    lock_chats();
    uint32_t hash = 2166136261u;
    for (size_t i = 0; i < catalog_size; ++i) hash = (hash ^ catalog[i]) * 16777619u;
    unlock_chats();
    return hash;
}

int sumi_chat_count(void)
{
    lock_chats();
    int count = catalog[1] + 1;
    unlock_chats();
    return count;
}

int sumi_chat_selected(void)
{
    lock_chats();
    int choice = selected;
    unlock_chats();
    return choice;
}

bool sumi_chat_select(int index)
{
    lock_chats();
    bool ok = index >= 0 && index <= catalog[1] &&
              (index == selected || save_catalog(catalog, catalog_size, index));
    if (ok) selected = index;
    unlock_chats();
    return ok;
}

void sumi_chat_id(char out[SUMI_CHAT_ID_BYTES + 1])
{
    lock_chats();
    const uint8_t *row = side_row(catalog, selected);
    if (row) memcpy(out, row, SUMI_CHAT_ID_BYTES);
    out[row ? SUMI_CHAT_ID_BYTES : 0] = 0;
    unlock_chats();
}

void sumi_chat_title(int index, char out[SUMI_CHAT_TITLE_BYTES + 1])
{
    lock_chats();
    const uint8_t *row = side_row(catalog, index);
    if (row) {
        unsigned length = row[SUMI_CHAT_ID_BYTES];
        memcpy(out, row + SUMI_CHAT_ID_BYTES + 1, length);
        out[length] = 0;
    } else strcpy(out, "メインチャット");
    unlock_chats();
}

bool sumi_chat_matches(const char *expected, const char *actual)
{
    return !expected[0] || (actual && !strcmp(expected, actual));
}
