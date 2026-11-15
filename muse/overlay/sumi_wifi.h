#pragma once
#include <stdbool.h>
bool sumi_wifi_request(void);
bool sumi_wifi_select(const char *ssid);
bool sumi_wifi_forget(const char *ssid);
void sumi_wifi_cancel(void);
bool sumi_wifi_active(void);
bool app_sumi_wifi_begin(void);
void app_sumi_wifi_leave(void);
void app_sumi_wifi_end(const char *joined);
/* Called by the existing Wi-Fi owner, never a second radio-control task. */
bool sumi_wifi_service(void);
void sumi_wifi_status(char *out, unsigned capacity);
