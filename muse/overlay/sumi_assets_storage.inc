#include <string.h>
#include "esp_partition.h"
#include "esp_crc.h"

#define BADGE_BYTES (400 * 400 * sizeof(uint16_t))
#define PORTRAIT_BYTES (360 * 360 * sizeof(uint16_t))

const uint16_t *sumi_badge;
const uint16_t *sumi_portrait;

bool sumi_assets_load(void)
{
    if (sumi_badge) return true;
    const esp_partition_t *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x42, "sumi_assets");
    const void *address;
    esp_partition_mmap_handle_t map;
    if (!part || esp_partition_mmap(part, 0, 8 + BADGE_BYTES + PORTRAIT_BYTES,
                                    ESP_PARTITION_MMAP_DATA, &address, &map) != ESP_OK) return false;
    const uint8_t *bytes = address;
    uint32_t expected;
    memcpy(&expected, bytes + 4, sizeof(expected));
    if (memcmp(bytes, "SUI1", 4) || esp_crc32_le(0, bytes + 8, BADGE_BYTES + PORTRAIT_BYTES) != expected) {
        esp_partition_munmap(map);
        return false;
    }
    sumi_badge = (const uint16_t *)(bytes + 8);
    sumi_portrait = (const uint16_t *)(bytes + 8 + BADGE_BYTES);
    return true;
}
