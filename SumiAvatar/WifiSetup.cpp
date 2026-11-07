#include "WifiSetup.h"

#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_random.h>

namespace WifiSetup {
namespace {
struct Credentials {
  char ssid[33];
  char password[65];
};

const IPAddress kSetupIp(192, 168, 4, 1);
const IPAddress kNetmask(255, 255, 255, 0);
constexpr uint32_t kSetupLimitMs = 300000;
constexpr uint32_t kConnectLimitMs = 20000;
WebServer server(kSetupIp, 80);
Credentials saved{}, candidate{};
char apName[24]{}, apPassword[13]{}, formToken[17]{};
const char* notice = "";
bool registered = false;
bool running = false;
bool testing = false;
bool finishing = false;
uint32_t startedAt = 0, testStartedAt = 0;

void join(const Credentials& credentials) {
  WiFi.disconnect();
  if (*credentials.ssid) {
    WiFi.begin(credentials.ssid, credentials.password);
  }
}

bool fromSetupNetwork() {
  return server.client().localIP() == kSetupIp &&
         (uint32_t(server.client().remoteIP()) & uint32_t(kNetmask)) ==
             (uint32_t(kSetupIp) & uint32_t(kNetmask));
}

void page() {
  if (!fromSetupNetwork()) {
    server.send(403, "text/plain", "Connect to the StopWatch Wi-Fi.");
    return;
  }
  String html = F("<!doctype html><html lang=en><meta charset=utf-8>"
                  "<meta name=viewport content='width=device-width, initial-scale=1'>"
                  "<title>StopWatch Wi-Fi</title><h1>StopWatch Wi-Fi</h1><p>");
  html += notice;
  html += F("</p>");
  if (testing || finishing) {
    if (testing) html += F("<meta http-equiv=refresh content='3;url=/'>");
  } else {
    html += F("<form method=post action=/wifi><input type=hidden name=token value='");
    html += formToken;
    html += F("'><label>2.4 GHz SSID <input name=ssid maxlength=32 required></label><br>"
              "<label>Password <input type=password name=password maxlength=64></label><br>"
              "<button>Connect</button></form>");
  }
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "text/html; charset=utf-8", html);
}

void submit() {
  if (!fromSetupNetwork() || server.arg("token") != formToken) {
    server.send(403, "text/plain", "Reload the setup page.");
    return;
  }
  if (!testing && !finishing) {
    const String ssid = server.arg("ssid");
    const String password = server.arg("password");
    const size_t length = password.length();
    const bool validPassword = length == 0 || (length >= 8 && length <= 63) ||
        (length == 64 && strspn(password.c_str(), "0123456789abcdefABCDEF") == 64);
    if (ssid.length() == 0 || ssid.length() > 32 || !validPassword) {
      notice = "Enter a valid SSID and password.";
    } else {
      ssid.toCharArray(candidate.ssid, sizeof(candidate.ssid));
      password.toCharArray(candidate.password, sizeof(candidate.password));
      notice = "Connecting. If the setup Wi-Fi disconnects, reconnect and reload.";
      testing = true;
      testStartedAt = millis();
      server.sendHeader("Location", "/");
      server.send(303, "text/plain", "");
      join(candidate);
      return;
    }
  }
  server.sendHeader("Location", "/");
  server.send(303, "text/plain", "");
}
}

void connect(const char* defaultSsid, const char* defaultPassword) {
  strlcpy(saved.ssid, defaultSsid, sizeof(saved.ssid));
  strlcpy(saved.password, defaultPassword, sizeof(saved.password));
  Preferences preferences;
  Credentials stored{};
  if (preferences.begin("sumi-wifi", true) &&
      preferences.getBytesLength("network") == sizeof(stored) &&
      preferences.getBytes("network", &stored, sizeof(stored)) == sizeof(stored) &&
      stored.ssid[0] && !stored.ssid[32] && !stored.password[64]) {
    saved = stored;
  }
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(true);
  join(saved);
}

bool start() {
  if (running) return true;
  snprintf(apName, sizeof(apName), "Sumi-Setup-%04lx", (unsigned long)(ESP.getEfuseMac() & 0xffff));
  snprintf(apPassword, sizeof(apPassword), "%08lx%04lx", (unsigned long)esp_random(),
           (unsigned long)(esp_random() & 0xffff));
  snprintf(formToken, sizeof(formToken), "%08lx%08lx", (unsigned long)esp_random(),
           (unsigned long)esp_random());
  if (!WiFi.mode(WIFI_AP_STA) || !WiFi.softAPConfig(kSetupIp, kSetupIp, kNetmask) ||
      !WiFi.softAP(apName, apPassword, 1, false, 1)) {
    WiFi.mode(WIFI_STA);
    return false;
  }
  if (!registered) {
    server.on("/", HTTP_GET, page);
    server.on("/wifi", HTTP_POST, submit);
    registered = true;
  }
  server.begin();
  notice = "Enter the Wi-Fi network the StopWatch should use.";
  running = true;
  startedAt = millis();
  return true;
}

void stop() {
  if (!running) return;
  server.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  if (testing || !connected()) join(saved);
  candidate = {};
  running = testing = finishing = false;
}

void update() {
  if (!running) return;
  server.handleClient();
  const uint32_t now = millis();
  if (now - startedAt >= kSetupLimitMs || (finishing && now - testStartedAt >= 4000)) {
    stop();
  } else if (testing && now - testStartedAt >= 1000 && connected() && WiFi.SSID() == candidate.ssid) {
    Preferences preferences;
    if (preferences.begin("sumi-wifi", false) &&
        preferences.putBytes("network", &candidate, sizeof(candidate)) == sizeof(candidate)) {
      saved = candidate;
      testing = false;
      finishing = true;
      testStartedAt = now;
      notice = "Connected. Setup Wi-Fi will close.";
    } else {
      testing = false;
      notice = "Could not save. Previous Wi-Fi restored.";
      join(saved);
    }
  } else if (testing && now - testStartedAt >= kConnectLimitMs) {
    testing = false;
    notice = "Connection failed. Previous Wi-Fi restored. Try again.";
    join(saved);
  }
}

bool active() { return running; }
bool connected() { return WiFi.status() == WL_CONNECTED; }
const char* accessPoint() { return apName; }
const char* accessPassword() { return apPassword; }
const char* status() {
  return finishing ? "Connected" : testing ? "Connecting..." : "Open http://192.168.4.1";
}
}
