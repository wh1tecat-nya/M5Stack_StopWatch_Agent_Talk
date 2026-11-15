#include "sumi_clock.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_log.h"

void sumi_clock_poll(bool connected) {
    static bool initialized, was_connected;
    static int64_t retry_at;
    int64_t now = esp_timer_get_time();
    if (!connected) { was_connected = false; return; }
    if (was_connected || now < retry_at) return;
    esp_err_t err;
    if (!initialized) {
        esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG("time.cloudflare.com");
        config.start = false;
        config.wait_for_sync = false;
        err = esp_netif_sntp_init(&config);
        if (err != ESP_OK) goto failed;
        initialized = true;
    }
    err = esp_netif_sntp_start();
    if (err != ESP_OK) goto failed;
    was_connected = true;
    return;
failed:
    retry_at = now + 60000000;
    ESP_LOGW("sumi_clock", "time sync start failed: %s", esp_err_to_name(err));
}
