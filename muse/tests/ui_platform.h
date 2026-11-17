#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#define MUSE_CAPTION_MAX 400
typedef enum { MUSE_MODE_BOOT, MUSE_MODE_IDLE, MUSE_MODE_LISTENING, MUSE_MODE_THINKING,
    MUSE_MODE_SPEAKING, MUSE_MODE_ERROR, MUSE_MODE_OFF } muse_mode_t;
typedef struct { int battery_pct, battery_mv; bool charging, usb; } muse_power_t;
typedef struct { unsigned passkey; } muse_ble_status_t;
enum { MUSE_LINK_CONFIRM = 3 };
typedef int nvs_handle_t;
typedef struct { char version[32]; } esp_app_desc_t;
const esp_app_desc_t *esp_app_get_description(void);
typedef int esp_err_t;
#define ESP_OK 0
#define NVS_READWRITE 1
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define ESP_LOGE(...) ((void)0)
int64_t esp_timer_get_time(void);
int nvs_set_i32(int, const char *, int);
int nvs_get_i32(int, const char *, int *);
int nvs_commit(int);
int nvs_open(const char *, int, int *);
const char *esp_err_to_name(int);
static inline void *heap_caps_malloc(size_t n, int flags) { (void)flags; return malloc(n); }
muse_mode_t muse_state_mode(float *);
void muse_state_set_asleep(bool);
bool muse_state_asleep(void);
void muse_state_poke(void);
float muse_state_level(void);
float muse_state_progress(void);
muse_power_t muse_state_power(void);
bool muse_state_caption(char *, size_t, uint32_t *);
void muse_state_set_page(int, int);
void muse_state_set_cjk_page(int, int);
void muse_state_set_latin_page(int, int);
bool muse_voice_in_call(void);
bool muse_settings_voice_notes(void);
void muse_settings_set_voice_notes(bool);
void muse_settings_set_speaker_on(bool);
int muse_settings_volume(void);
void muse_settings_set_volume(int);
bool muse_link_hatch_linked(void);
int muse_link_state(void);
void muse_ble_status(muse_ble_status_t *);
bool muse_wifi_connected(void);
#define MUSE_SSID_MAX 32
#define MUSE_WIFI_SAVED_MAX 8
typedef enum { MUSE_WIFI_OFF, MUSE_WIFI_CONNECTED } muse_wifi_state_t;
typedef struct { muse_wifi_state_t state; char ssid[MUSE_SSID_MAX + 1]; } muse_wifi_status_t;
typedef struct { char ssid[MUSE_SSID_MAX + 1]; bool hidden; } muse_wifi_saved_t;
void muse_wifi_status(muse_wifi_status_t *out);
int muse_wifi_saved(muse_wifi_saved_t *out, int max);

bool muse_voice_notes_waiting(void);
int muse_settings_mic_gain(void);
void muse_settings_set_mic_gain(int);
