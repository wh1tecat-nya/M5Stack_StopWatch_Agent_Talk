#pragma once
#include <stdbool.h>
#include <stdint.h>
void sumi_ui_init(void);
void sumi_ui_prepare(void);
bool sumi_ui_tick(void);
int sumi_ui_brightness(int fallback);
void sumi_ui_wake(void);
void sumi_ui_toggle_settings(void);
bool sumi_ui_settings_open(void);
void sumi_input_end_call(void);
void sumi_mic_test_request(void);
bool sumi_mic_test_active(void);
int sumi_ui_setting(unsigned key);
int sumi_ui_apply_setting(unsigned key, int32_t value);
