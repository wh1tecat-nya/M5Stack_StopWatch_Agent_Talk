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

uint32_t sumi_phone_begin(void);
bool sumi_phone_current(uint32_t turn);
void sumi_phone_end(uint32_t turn, bool cancel);
const char *sumi_phone_exchange(uint32_t turn, const int16_t *pcm, size_t frames,
                                bool (*interrupted)(void));

extern uint16_t sumi_phone_tx_handle;
int sumi_phone_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg);
void sumi_phone_gap_event(struct ble_gap_event *event);
int sumi_phone_settings_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg);
