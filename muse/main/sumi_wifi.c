#include "sumi_wifi.h"
#include "muse_settings.h"
#include "muse_state.h"
#include "muse_voice.h"
#include "muse_wifi.h"
#include "wifi_mgr.h"
#include "wifi_known.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

typedef struct { char ssid[33], password[65]; } credentials_t;
typedef enum { WIFI_NONE, WIFI_ADD, WIFI_SELECT, WIFI_FORGET } wifi_action_t;
typedef struct { wifi_action_t action; char ssid[WIFI_KNOWN_SSID_MAX + 1]; } wifi_request_t;
static wifi_request_t pending;
static bool running;
static atomic_bool cancelled;
static QueueHandle_t submissions;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static char message[256] = "準備中…", csrf[17];
static esp_netif_t *ap;
static void status(const char *format, ...) {
    char text[256]; va_list args; va_start(args, format);
    vsnprintf(text, sizeof(text), format, args); va_end(args);
    portENTER_CRITICAL(&lock); memcpy(message, text, sizeof(message)); portEXIT_CRITICAL(&lock);
}
void sumi_wifi_status(char *out, unsigned cap) {
    portENTER_CRITICAL(&lock); strlcpy(out, message, cap); portEXIT_CRITICAL(&lock);
}
static bool request(wifi_action_t action, const char *ssid) {
    if (action != WIFI_ADD && (!ssid || !ssid[0] || strlen(ssid) > WIFI_KNOWN_SSID_MAX)) return false;
    portENTER_CRITICAL(&lock);
    bool accepted = pending.action == WIFI_NONE;
    if (accepted) {
        pending.action = action;
        strlcpy(pending.ssid, ssid ? ssid : "", sizeof(pending.ssid));
        strlcpy(message, "準備中…", sizeof(message));
        atomic_store(&cancelled, false);
    }
    portEXIT_CRITICAL(&lock);
    if (accepted) muse_wifi_apply();
    return accepted;
}
bool sumi_wifi_request(void) { return request(WIFI_ADD, NULL); }
bool sumi_wifi_select(const char *ssid) { return request(WIFI_SELECT, ssid); }
bool sumi_wifi_forget(const char *ssid) { return request(WIFI_FORGET, ssid); }
void sumi_wifi_cancel(void) {
    portENTER_CRITICAL(&lock);
    if (pending.action == WIFI_ADD) {
        atomic_store(&cancelled, true);
        if (!running) pending.action = WIFI_NONE;
    }
    portEXIT_CRITICAL(&lock);
}
bool sumi_wifi_active(void) {
    portENTER_CRITICAL(&lock);
    bool active = pending.action != WIFI_NONE;
    portEXIT_CRITICAL(&lock);
    return active;
}
static int hex(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static bool field(const char *body, const char *key, char *out, size_t cap) {
    char encoded[256];
    if (httpd_query_key_value(body, key, encoded, sizeof(encoded)) != ESP_OK) return false;
    size_t n = 0;
    for (size_t i = 0; encoded[i]; ++i) {
        unsigned char c = encoded[i];
        if (c == '+') c = ' ';
        else if (c == '%') {
            if (!encoded[i+1] || !encoded[i+2] || hex(encoded[i+1]) < 0 || hex(encoded[i+2]) < 0) return false;
            c = (hex(encoded[i+1]) << 4) | hex(encoded[i+2]); i += 2;
        }
        if (!c || n + 1 >= cap) return false;
        out[n++] = c;
    }
    out[n] = 0; return true;
}
static esp_err_t page(httpd_req_t *r) {
    char html[1900];
    snprintf(html, sizeof(html), "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>すみのWi-Fi設定</title><style>body{font:18px sans-serif;max-width:32em;margin:3em auto;padding:1em}"
        "input,button{font:inherit;width:100%%;padding:.6em;box-sizing:border-box}label{display:block;margin:1em 0}</style>"
        "<h1>すみのWi-Fi設定</h1><p>2.4 GHzのネットワークを指定してください。接続できた設定を最大8件保存します。同じ名前ならパスワードを更新します。</p>"
        "<form method=post action=/connect><input type=hidden name=token value='%s'>"
        "<label>ネットワーク名<input name=ssid required maxlength=32 autocomplete=off></label>"
        "<label>パスワード<input type=password name=password maxlength=64 autocomplete=off></label>"
        "<button>接続を試す</button></form><p>結果はStopWatchの画面に表示されます。設定用Wi-Fiは5分で閉じます。</p>", csrf);
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_sendstr(r, html);
}
static esp_err_t submit(httpd_req_t *r) {
    char body[512], token[32]; credentials_t creds = {0};
    if (r->content_len <= 0 || r->content_len >= sizeof(body))
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "Invalid form");
    size_t n = 0;
    while (n < r->content_len) {
        int got = httpd_req_recv(r, body + n, r->content_len - n);
        if (got <= 0) return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "Incomplete form");
        n += got;
    }
    body[n] = 0;
    bool valid = field(body, "token", token, sizeof(token)) && !strcmp(token, csrf)
        && field(body, "ssid", creds.ssid, sizeof(creds.ssid)) && creds.ssid[0]
        && field(body, "password", creds.password, sizeof(creds.password));
    size_t length = strlen(creds.password);
    valid = valid && (!length || length >= 8);
    if (length == 64) for (size_t i = 0; i < length; ++i) if (hex(creds.password[i]) < 0) valid = false;
    bool queued = valid && xQueueSend(submissions, &creds, 0) == pdTRUE;
    memset(&creds, 0, sizeof(creds)); memset(body, 0, sizeof(body));
    if (!queued) return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "Invalid form or connection in progress");
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_sendstr(r, "<meta charset=utf-8><p>接続を試しています。StopWatchで結果を確認してください。</p><a href=/>戻る</a>");
}
static int find_network(const wifi_known_list_t *saved, const char *ssid) {
    for (int i = 0; i < saved->count; ++i) {
        if (strcmp(saved->nets[i].ssid, ssid) == 0) return i;
    }
    return -1;
}

static void manage_network(const wifi_request_t *work, wifi_known_list_t *saved, char *joined) {
    int index = find_network(saved, work->ssid);
    if (index < 0) {
        status("保存済みのWi-Fiが\n見つかりません。");
        return;
    }
    muse_wifi_status_t current;
    muse_wifi_status(&current);
    bool in_use = current.state == MUSE_WIFI_CONNECTED && strcmp(current.ssid, work->ssid) == 0;
    if (work->action == WIFI_FORGET) {
        if (!wifi_known_forget(work->ssid)) {
            status("Wi-Fiを削除できませんでした。");
            return;
        }
        if (in_use) app_sumi_wifi_leave();
        status("保存済みWi-Fiを削除しました。");
        return;
    }
    if (in_use) {
        status("このWi-Fiに接続中です。");
        return;
    }
    app_sumi_wifi_leave();
    status("選んだWi-Fiへ接続中…");
    if (!wifi_mgr_connect(work->ssid, saved->nets[index].password, 15000)) {
        wifi_mgr_disconnect();
        status("接続できませんでした。\n保存済みWi-Fiへ再接続します。");
        return;
    }
    if (index > 0 && !wifi_known_remember(work->ssid, saved->nets[index].password, -1)) {
        wifi_mgr_disconnect();
        status("切り替えを保存できませんでした。\n保存済みWi-Fiへ再接続します。");
        return;
    }
    strlcpy(joined, work->ssid, WIFI_KNOWN_SSID_MAX + 1);
    status("選んだWi-Fiに接続しました。");
}

static void configure(wifi_known_list_t *saved, char *joined) {
    app_sumi_wifi_leave();
    httpd_handle_t server = NULL;
    if (!submissions) submissions = xQueueCreate(1, sizeof(credentials_t));
    if (!ap) ap = esp_netif_create_default_wifi_ap();
    wifi_config_t config = {0};
    snprintf((char *)config.ap.ssid, sizeof(config.ap.ssid), "SUMI-SETUP-%04X", (unsigned)(esp_random() & 0xffff));
    snprintf((char *)config.ap.password, sizeof(config.ap.password), "%08lx", (unsigned long)esp_random());
    snprintf(csrf, sizeof(csrf), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    config.ap.ssid_len = strlen((char *)config.ap.ssid); config.ap.channel = 1;
    config.ap.max_connection = 1; config.ap.authmode = WIFI_AUTH_WPA2_PSK;
    if (!submissions || !ap || esp_wifi_set_mode(WIFI_MODE_APSTA) != ESP_OK ||
        esp_wifi_set_config(WIFI_IF_AP, &config) != ESP_OK) goto finish;
    httpd_config_t hc = HTTPD_DEFAULT_CONFIG(); hc.stack_size = 6144; hc.max_open_sockets = 2;
    hc.recv_wait_timeout = 3; hc.lru_purge_enable = true;
    if (httpd_start(&server, &hc) != ESP_OK) goto finish;
    httpd_uri_t get = {.uri="/", .method=HTTP_GET, .handler=page};
    httpd_uri_t post = {.uri="/connect", .method=HTTP_POST, .handler=submit};
    if (httpd_register_uri_handler(server, &get) != ESP_OK || httpd_register_uri_handler(server, &post) != ESP_OK) {
        httpd_stop(server);
        server = NULL;
        goto finish;
    }
    int64_t deadline = esp_timer_get_time() + 300LL * 1000000;
    status("Wi-Fi: %s\nパスワード: %s\n\nhttp://192.168.4.1\nを開いてください。", config.ap.ssid, config.ap.password);
    credentials_t next = {0};
    while (!atomic_load(&cancelled) && esp_timer_get_time() < deadline) {
        if (xQueueReceive(submissions, &next, pdMS_TO_TICKS(100)) != pdTRUE) continue;
        if (saved->count >= WIFI_KNOWN_MAX && find_network(saved, next.ssid) < 0) {
            status("Wi-Fiは最大8件です。\n保存済みの1件を削除してください。");
            memset(&next, 0, sizeof(next));
            break;
        }
        status("新しいWi-Fiへ接続中…");
        int remaining = (int)((deadline - esp_timer_get_time()) / 1000);
        bool connected = remaining > 0 && wifi_mgr_connect(next.ssid, next.password, remaining < 15000 ? remaining : 15000);
        if (connected && !atomic_load(&cancelled)) {
            if (wifi_known_remember(next.ssid, next.password, !wifi_mgr_cached_scan_has(next.ssid))) {
                strlcpy(joined, next.ssid, WIFI_KNOWN_SSID_MAX + 1);
                status("接続できました。\n設定を保存しました。");
            } else status("接続できましたが\n保存に失敗しました。");
            memset(&next, 0, sizeof(next));
            break;
        } else status("接続できませんでした。\nスマートフォンで再入力できます。\n\nWi-Fi: %s\nパスワード: %s\nhttp://192.168.4.1", config.ap.ssid, config.ap.password);
        memset(&next, 0, sizeof(next));
    }
    if (!joined[0] && (atomic_load(&cancelled) || esp_timer_get_time() >= deadline))
        status("設定用Wi-Fiを閉じました。\n保存済みWi-Fiへ再接続します。");
finish:
    if (!server) status("設定用Wi-Fiを開始できませんでした。");
    if (server) httpd_stop(server);
    if (submissions) { credentials_t unused; while (xQueueReceive(submissions, &unused, 0) == pdTRUE) memset(&unused, 0, sizeof(unused)); }
    esp_wifi_set_mode(WIFI_MODE_STA);
    memset(&config, 0, sizeof(config)); memset(csrf, 0, sizeof(csrf));
    if (!joined[0]) wifi_mgr_disconnect();
}

bool sumi_wifi_service(void) {
    if (!sumi_wifi_active() || !app_sumi_wifi_begin()) return false;
    portENTER_CRITICAL(&lock);
    wifi_request_t work = pending;
    running = work.action != WIFI_NONE;
    portEXIT_CRITICAL(&lock);

    char joined[WIFI_KNOWN_SSID_MAX + 1] = {0};
    muse_mode_t mode = muse_state_mode(NULL);
    if (running && (muse_voice_in_call() || muse_voice_notes_waiting() || mode == MUSE_MODE_LISTENING ||
                    mode == MUSE_MODE_THINKING || mode == MUSE_MODE_SPEAKING)) {
        status("会話が終わってから\nもう一度操作してください。");
    } else if (running) {
        wifi_known_list_t *saved = malloc(sizeof(*saved));
        if (saved) {
            wifi_known_load(saved);
            if (work.action == WIFI_ADD) configure(saved, joined);
            else manage_network(&work, saved, joined);
            wifi_known_wipe(saved);
            free(saved);
        } else status("Wi-Fi設定を読み込めませんでした。");
    }
    if (joined[0] && !muse_settings_wifi_on()) muse_settings_set_wifi_on(true);
    app_sumi_wifi_end(joined);
    portENTER_CRITICAL(&lock);
    if (running) pending.action = WIFI_NONE;
    running = false;
    portEXIT_CRITICAL(&lock);
    return true;
}
