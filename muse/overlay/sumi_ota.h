#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SUMI_OTA_TAG 0xa7
#define SUMI_OTA_PACKET_MAX 512

bool sumi_ota_busy(void);
void sumi_ota_disconnect(void);
int sumi_ota_command(const uint8_t *data, size_t size, bool idle);
size_t sumi_ota_status(uint8_t out[16]);
