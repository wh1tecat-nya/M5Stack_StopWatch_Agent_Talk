#pragma once

#include <stdbool.h>
#include <stdint.h>

#define SUMI_MUSE_TOKEN_CAP 2048
#define SUMI_MUSE_PEER_CAP 16
#define SUMI_MUSE_REV_BYTES 8

typedef struct {
    char access[SUMI_MUSE_TOKEN_CAP];
    char refresh[SUMI_MUSE_TOKEN_CAP];
    uint8_t reserved;
} sumi_muse_pair_t;

// Access to this record is serialized by the SDK's auth mutex.
bool config_sumi_muse_load(sumi_muse_pair_t *pair);
bool config_sumi_muse_store(const sumi_muse_pair_t *pair);
