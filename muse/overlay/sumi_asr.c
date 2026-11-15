#include "sumi_asr.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "nvs.h"
#include "cJSON.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define KEY_CAP 512
#define RESPONSE_CAP 16384
static atomic_bool enabled, configured;
static const char prefix[] =
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ngpt-transcribe\r\n"
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"languages[]\"\r\n\r\nja\r\n"
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"keywords[]\"\r\n\r\nM5Stack\r\n"
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"keywords[]\"\r\n\r\nMuse\r\n"
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"keywords[]\"\r\n\r\nStopWatch\r\n"
    "--sumi-asr-2026\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n"
    "Content-Type: audio/wav\r\n\r\n";
static const char suffix[] = "\r\n--sumi-asr-2026--\r\n";

bool sumi_asr_key_valid(const char *key) {
    size_t n = key ? strlen(key) : 0;
    if (n < 20 || n >= KEY_CAP || strncmp(key, "sk-", 3)) return false;
    for (size_t i = 0; i < n; ++i) {
        char c = key[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
    }
    return true;
}
void sumi_asr_init(void) {
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READONLY, &nvs) != ESP_OK) return;
    size_t n = 0; int32_t mode = 0;
    bool key = nvs_get_str(nvs, "asr_key", NULL, &n) == ESP_OK && n > 20 && n <= KEY_CAP;
    nvs_get_i32(nvs, "asr", &mode); nvs_close(nvs);
    atomic_store(&configured, key); atomic_store(&enabled, key && mode == 1);
}
bool sumi_asr_enabled(void) { return atomic_load(&enabled); }
bool sumi_asr_configured(void) { return atomic_load(&configured); }
bool sumi_asr_select(bool on) {
    if (on && !sumi_asr_configured()) return false;
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READWRITE, &nvs) != ESP_OK) return false;
    esp_err_t err = nvs_set_i32(nvs, "asr", on);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    if (err == ESP_OK) atomic_store(&enabled, on);
    return err == ESP_OK;
}
bool sumi_asr_set_key(const char *key) {
    if (!sumi_asr_key_valid(key)) return false;
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READWRITE, &nvs) != ESP_OK) return false;
    esp_err_t err = nvs_set_str(nvs, "asr_key", key);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    if (err == ESP_OK) atomic_store(&configured, true);
    return err == ESP_OK;
}
static void le32(uint8_t *p, uint32_t n) {
    for (int i = 0; i < 4; ++i) p[i] = (uint8_t)(n >> (8 * i));
}
static void wave_header(uint8_t *h, size_t frames) {
    memset(h, 0, 44); memcpy(h, "RIFF", 4); le32(h + 4, frames * 2 + 36);
    memcpy(h + 8, "WAVEfmt ", 8); le32(h + 16, 16); h[20] = 1; h[22] = 1;
    le32(h + 24, 16000); le32(h + 28, 32000); h[32] = 2; h[34] = 16;
    memcpy(h + 36, "data", 4); le32(h + 40, frames * 2);
}
static bool interrupted(bool (*cancelled)(void), int64_t until) {
    return (cancelled && cancelled()) || esp_timer_get_time() >= until;
}
static bool write_all(esp_http_client_handle_t http, const void *data, size_t size,
                      bool (*cancelled)(void), int64_t until) {
    const char *p = data;
    while (size) {
        if (interrupted(cancelled, until)) return false;
        int n = esp_http_client_write(http, p, size > 4096 ? 4096 : size);
        if (n <= 0) return false;
        p += n; size -= n;
    }
    return true;
}
bool sumi_asr_transcribe(const int16_t *pcm, size_t frames, char *out, size_t cap,
                        bool (*cancelled)(void), char *why, size_t why_cap) {
    if (!out || !cap || !why || !why_cap) return false;
    out[0] = 0;
    snprintf(why, why_cap, "音声認識に接続できません");
    if (!pcm || !frames || frames > 15 * 16000) return false;
    char key[KEY_CAP] = {0}, auth[KEY_CAP + 8] = {0}; size_t key_size = sizeof(key);
    nvs_handle_t nvs;
    if (nvs_open("sumi-muse", NVS_READONLY, &nvs) != ESP_OK) return false;
    esp_err_t err = nvs_get_str(nvs, "asr_key", key, &key_size); nvs_close(nvs);
    if (err != ESP_OK || !sumi_asr_key_valid(key)) {
        memset(key, 0, sizeof(key)); snprintf(why, why_cap, "OpenAIキーを設定してください"); return false;
    }
    snprintf(auth, sizeof(auth), "Bearer %s", key); memset(key, 0, sizeof(key));
    esp_http_client_config_t config = {.url = "https://api.openai.com/v1/audio/transcriptions",
        .method = HTTP_METHOD_POST, .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 8000, .disable_auto_redirect = true, .buffer_size = 2048, .buffer_size_tx = 2048};
    esp_http_client_handle_t http = esp_http_client_init(&config);
    char *response = NULL; bool ok = false;
    int64_t start = esp_timer_get_time(), until = start + 45000000;
    if (!http) goto finish;
    if (esp_http_client_set_header(http, "Authorization", auth) != ESP_OK ||
        esp_http_client_set_header(http, "Content-Type", "multipart/form-data; boundary=sumi-asr-2026") != ESP_OK) goto finish;
    memset(auth, 0, sizeof(auth));
    uint8_t wav[44]; wave_header(wav, frames);
    size_t total = sizeof(prefix) - 1 + sizeof(wav) + frames * 2 + sizeof(suffix) - 1;
    if (interrupted(cancelled, until) || esp_http_client_open(http, total) != ESP_OK ||
        !write_all(http, prefix, sizeof(prefix) - 1, cancelled, until) ||
        !write_all(http, wav, sizeof(wav), cancelled, until) ||
        !write_all(http, pcm, frames * 2, cancelled, until) ||
        !write_all(http, suffix, sizeof(suffix) - 1, cancelled, until)) goto finish;
    if (esp_http_client_fetch_headers(http) < 0 || interrupted(cancelled, until)) goto finish;
    int status = esp_http_client_get_status_code(http);
    ESP_LOGI("sumi_asr", "response HTTP %d", status);
    if (status != 200) {
        snprintf(why, why_cap, status == 401 ? "OpenAIキーを確認してください（401）" :
            status == 429 ? "OpenAIの利用枠・制限を確認（429）" : "音声認識エラー（HTTP %d）", status);
        goto finish;
    }
    response = malloc(RESPONSE_CAP);
    if (!response) { snprintf(why, why_cap, "音声認識のメモリー不足"); goto finish; }
    size_t n = 0;
    while (n < RESPONSE_CAP - 1 && !interrupted(cancelled, until)) {
        int got = esp_http_client_read(http, response + n, RESPONSE_CAP - 1 - n);
        if (got < 0) goto finish;
        if (!got) break;
        n += got;
    }
    if (interrupted(cancelled, until) || !esp_http_client_is_complete_data_received(http)) goto finish;
    response[n] = 0;
    cJSON *json = cJSON_Parse(response);
    const char *text = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(json, "text"));
    if (text && *text && strlen(text) < cap) {
        memcpy(out, text, strlen(text) + 1); ok = true;
    } else snprintf(why, why_cap, "認識結果が空か長すぎます。もう一度話してね");
    cJSON_Delete(json);
finish:
    memset(auth, 0, sizeof(auth));
    free(response);
    if (http) esp_http_client_cleanup(http);
    if (interrupted(cancelled, until)) snprintf(why, why_cap, "音声認識を中断しました");
    ESP_LOGI("sumi_asr", "transcription %s: %.2fs, audio %.2fs", ok ? "ok" : "failed",
             (esp_timer_get_time() - start) / 1000000.0, frames / 16000.0);
    return ok;
}
