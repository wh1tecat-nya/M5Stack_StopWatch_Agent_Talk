#pragma once

// Copy to stopwatch_config.h and fill in values locally.
static constexpr char kWifiSsid[] = "YOUR_2_4_GHZ_SSID";
static constexpr char kWifiPassword[] = "YOUR_WIFI_PASSWORD";
static constexpr char kRelayUrl[] = "https://relay.example.com";  // No trailing slash.
static constexpr char kRelayToken[] = "YOUR_SUMI_DEVICE_TOKEN";
static constexpr char kRelayRootCa[] = R"PEM(
-----BEGIN CERTIFICATE-----
YOUR_RELAY_ROOT_CA
-----END CERTIFICATE-----
)PEM";
