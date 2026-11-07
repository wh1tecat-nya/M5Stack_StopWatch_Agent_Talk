#include "VoiceLink.h"
#include "LocalSpeech.h"
#include "WifiSetup.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_random.h>

#if __has_include("stopwatch_config.h")
#include "stopwatch_config.h"  // Defines the five kWifi/kRelay constants below.
#else
static constexpr char kWifiSsid[] = "";
static constexpr char kWifiPassword[] = "";
static constexpr char kRelayUrl[] = "";
static constexpr char kRelayToken[] = "";
static constexpr char kRelayRootCa[] = "";
#endif

namespace VoiceLink {
namespace {
QueueHandle_t requests;
QueueHandle_t results;
int16_t* recording;
int16_t* playback;

bool validId(const String& id) {
  return id.length() == 32 && strspn(id.c_str(), "0123456789abcdef") == 32;
}

void exchange(Result& result) {
  result.status = -1;
  if (!connected()) {
    return;
  }

  WiFiClientSecure client;
  client.setCACert(kRelayRootCa);
  client.setHandshakeTimeout(5);
  HTTPClient http;
  http.setConnectTimeout(4000);
  http.setTimeout(5000);
  http.useHTTP10(true);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);

  String url = kRelayUrl;
  switch (result.request.job) {
    case Job::Upload: url += "/device/turns/"; url += result.request.id; break;
    case Job::Poll: url += "/device/replies/next"; break;
    case Job::Acknowledge: url += "/device/replies/"; url += result.request.id; break;
  }
  if (!http.begin(client, url)) {
    return;
  }

  http.addHeader("Authorization", String("Bearer ") + kRelayToken);
  if (result.request.job == Job::Upload) {
    http.addHeader("Content-Type", "application/octet-stream");
    result.status = http.PUT(reinterpret_cast<uint8_t*>(recording),
                             result.request.samples * sizeof(int16_t));
    result.receivedMs = millis();
  } else if (result.request.job == Job::Acknowledge) {
    result.status = http.sendRequest("DELETE");
  } else {
    const char* keys[] = {"X-Reply-Id", "Content-Type"};
    http.collectHeaders(keys, 2);
    result.status = http.GET();
    result.receivedMs = millis();
    if (result.status == 422) {
      const String id = http.header("X-Reply-Id");
      if (!validId(id)) {
        result.status = -2;
        return;
      }
      id.toCharArray(result.request.id, sizeof(result.request.id));
    }
    if (result.status == 200) {
      const int length = http.getSize();
      const String id = http.header("X-Reply-Id");
      const String type = http.header("Content-Type");
      const bool textReply = type == "text/plain; charset=utf-8";
      if (!validId(id) || length <= 0) {
        result.status = -2;
        return;
      }
      id.toCharArray(result.request.id, sizeof(result.request.id));
      if (textReply && size_t(length) >= sizeof(result.text)) {
        result.status = 415;
        return;
      }
      if (!textReply && (type != "application/octet-stream" || length % 2 ||
                         size_t(length) > kPlaybackSamples * sizeof(int16_t))) {
        result.status = -2;
        return;
      }

      auto* stream = http.getStreamPtr();
      if (!stream) {
        result.status = -2;
        return;
      }
      size_t received = 0;
      const uint32_t start = millis();
      auto* bytes = reinterpret_cast<uint8_t*>(textReply ? static_cast<void*>(result.text) : playback);
      while (received < size_t(length) && millis() - start < 12000) {
        const int available = stream->available();
        if (available > 0) {
          const int count = stream->read(bytes + received, min(size_t(available), length - received));
          if (count > 0) {
            received += count;
          }
        } else if (!http.connected()) {
          break;
        } else {
          delay(5);
        }
      }
      if (received != size_t(length)) {
        result.status = -2;
        return;
      }
      if (textReply) {
        result.text[received] = '\0';
        http.end();
        const uint32_t synthesisStarted = millis();
        result.samples = LocalSpeech::synthesize(result.text, playback, kPlaybackSamples);
        result.synthesisMs = millis() - synthesisStarted;
        result.sampleRate = LocalSpeech::kSampleRate;
        if (!result.samples) result.status = 415;
      } else {
        result.samples = received / sizeof(int16_t);
        result.sampleRate = kPlaybackRate;
      }
    }
  }
}

void worker(void*) {
  for (;;) {
    Result result{};
    xQueueReceive(requests, &result.request, portMAX_DELAY);
    exchange(result);
    xQueueSend(results, &result, portMAX_DELAY);
  }
}
}

bool begin(int16_t* input, int16_t* output) {
  WifiSetup::connect(kWifiSsid, kWifiPassword);
  if (strncmp(kRelayUrl, "https://", 8) || !*kRelayToken || !*kRelayRootCa) {
    return false;
  }
  recording = input;
  playback = output;
  requests = xQueueCreate(1, sizeof(Request));
  results = xQueueCreate(1, sizeof(Result));
  if (!requests || !results || xTaskCreate(worker, "voice-link", 12288, nullptr, 1, nullptr) != pdPASS) {
    if (requests) vQueueDelete(requests);
    if (results) vQueueDelete(results);
    requests = results = nullptr;
    return false;
  }
  configTime(0, 0, "time.cloudflare.com", "pool.ntp.org");
  return true;
}

bool connected() {
  return requests && WifiSetup::connected() && time(nullptr) >= 1700000000;
}

bool send(const Request& request) {
  return requests && xQueueSend(requests, &request, 0) == pdTRUE;
}

bool receive(Result& result) {
  return results && xQueueReceive(results, &result, 0) == pdTRUE;
}

void newId(char (&id)[33]) {
  uint8_t bytes[16];
  esp_fill_random(bytes, sizeof(bytes));
  for (size_t index = 0; index < sizeof(bytes); ++index) {
    snprintf(id + index * 2, 3, "%02x", unsigned(bytes[index]));
  }
}
}
