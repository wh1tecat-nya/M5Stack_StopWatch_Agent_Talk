#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct ble_gap_event;
struct ble_gatt_access_ctxt;

void sumi_phone_load_mode(void);
bool sumi_phone_mode(void);
bool sumi_phone_set_mode(bool enabled);
bool sumi_phone_ready(void);
bool sumi_phone_settings_enabled(void);
void sumi_phone_open_settings(void);
void sumi_phone_apply_setting(void);
void sumi_phone_allow_muse_sync(void);

#define SUMI_MUSE_ACCESS_MAX 2047
#define SUMI_MUSE_ACCESS_DIGEST 32
int app_sumi_muse_read_access(char *out, size_t capacity);
int app_sumi_muse_replace_access(const char *token, const uint8_t *expected_digest);
bool app_sumi_muse_refresh_pending(void);
void app_sumi_muse_load_pending(void);
int app_sumi_muse_read_pair(const char *peer, bool allow_enroll, uint8_t *out, size_t capacity);
int app_sumi_muse_reserve(const char *peer, const uint8_t *revision, bool reserve);
int app_sumi_muse_commit(const char *peer, const uint8_t *bytes, size_t length);

uint32_t sumi_phone_begin(void);
uint32_t sumi_phone_take_push(void);
bool sumi_phone_current(uint32_t turn);
void sumi_phone_end(uint32_t turn, bool cancel);
void sumi_phone_note_progress(uint32_t turn, const int16_t *pcm, size_t frames);
const char *sumi_phone_exchange(uint32_t turn, const int16_t *pcm, size_t frames,
                                bool (*interrupted)(void));
const char *sumi_phone_play_push(uint32_t turn, bool (*interrupted)(void));

extern uint16_t sumi_phone_tx_handle;
int sumi_phone_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg);
void sumi_phone_gap_event(struct ble_gap_event *event);
int sumi_phone_settings_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg);
