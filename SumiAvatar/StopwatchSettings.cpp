#include "StopwatchSettings.h"
#include "WifiSetup.h"

#include <Preferences.h>

namespace StopwatchSettings {
Values values;

namespace {
constexpr uint8_t kDurations[] = {15, 30, 45, 60};
constexpr int kSliderStart = 164;
constexpr int kSliderEnd = 304;
constexpr int kSliderTop = 90;
constexpr int kSliderSpacing = 37;
constexpr int kDurationTop = 213;
constexpr int kDurationSpacing = 37;
constexpr int kChoiceStart = 153;
constexpr int kChoiceSpacing = 47;
constexpr int kWifiTop = 326;
constexpr int kMinLight = 24;
constexpr const char* kSliderLabels[] = {"Volume", "Avatar LCD", "Badge LCD"};
constexpr const char* kDurationLabels[] = {"Avatar", "Badge lit", "Badge dim"};
uint8_t* const sliders[] = {&values.volume, &values.avatarLight, &values.badgeLight};
uint8_t* const durations[] = {&values.avatarSeconds, &values.badgeSeconds, &values.dimSeconds};
int dragging = -1;
bool dirty = false;
bool confirming = false;
bool wifiWasActive = false;
const char* wifiNotice = "";
const char* previousWifiStatus = nullptr;

bool validTime(uint8_t value) {
  return value == 15 || value == 30 || value == 45 || value == 60;
}

void save() {
  if (!dirty) return;
  Preferences preferences;
  if (preferences.begin("sumi-display", false) &&
      preferences.putBytes("values", &values, sizeof(values)) == sizeof(values)) {
    dirty = false;
  }
}

void label(M5Canvas& frame, const char* text, int x, int y, bool centered = false) {
  frame.setTextDatum(centered ? middle_center : middle_left);
  frame.drawString(text, x, y);
}

void button(M5Canvas& frame, const char* text, int x, int y, int width, bool selected = false) {
  frame.fillRoundRect(x, y, width, 28, 7, selected ? TFT_NAVY : TFT_LIGHTGREY);
  frame.setTextColor(selected ? TFT_WHITE : TFT_BLACK);
  label(frame, text, x + width / 2, y + 14, true);
  frame.setTextColor(TFT_BLACK);
}
}

void load() {
  Preferences preferences;
  Values stored;
  if (preferences.begin("sumi-display", true) &&
      preferences.getBytesLength("values") == sizeof(stored) &&
      preferences.getBytes("values", &stored, sizeof(stored)) == sizeof(stored) &&
      stored.avatarLight >= kMinLight && stored.badgeLight >= kMinLight &&
      validTime(stored.avatarSeconds) && validTime(stored.badgeSeconds) && validTime(stored.dimSeconds)) {
    values = stored;
  }
}

void open() {
  dragging = -1;
  confirming = false;
  wifiNotice = "";
  previousWifiStatus = nullptr;
}

void close() {
  save();
  WifiSetup::stop();
  wifiWasActive = false;
}

bool update(int x, int y, bool pressed, bool firstPress, bool canChangeWifi) {
  WifiSetup::update();
  const bool wifiActive = WifiSetup::active();
  const bool wifiChanged = wifiActive != wifiWasActive;
  wifiWasActive = wifiActive;
  if (wifiActive) {
    if (firstPress && y >= kWifiTop && y < kWifiTop + 28 && x >= 144 && x < 256) {
      WifiSetup::stop();
      wifiWasActive = false;
      return true;
    }
    const char* status = WifiSetup::status();
    const bool statusChanged = status != previousWifiStatus;
    previousWifiStatus = status;
    return wifiChanged || statusChanged;
  }
  if (!pressed && dragging >= 0) {
    dragging = -1;
    save();
  }
  if (confirming) {
    if (!firstPress || y < 225 || y >= 253 || x < 95 || x >= 305) return false;
    confirming = false;
    if (x >= 207) {
      if (!canChangeWifi) {
        wifiNotice = "Wi-Fi busy. Try again.";
      } else if (!WifiSetup::start()) {
        wifiNotice = "Could not start Wi-Fi setup";
      }
    }
    return true;
  }
  if (firstPress && x >= kSliderStart - 12 && x <= kSliderEnd + 12) {
    for (int index = 0; index < 3; ++index) {
      if (abs(y - (kSliderTop + index * kSliderSpacing)) <= 13) dragging = index;
    }
  }
  if (pressed && dragging >= 0) {
    const int minimum = dragging == 0 ? 0 : kMinLight;
    const uint8_t value = map(constrain(x, kSliderStart, kSliderEnd), kSliderStart, kSliderEnd, minimum, 255);
    if (*sliders[dragging] == value) return false;
    *sliders[dragging] = value;
    dirty = true;
    return true;
  }
  if (firstPress) {
    for (int row = 0; row < 3; ++row) {
      const int top = kDurationTop + row * kDurationSpacing;
      if (y < top || y >= top + 28) continue;
      for (int column = 0; column < 4; ++column) {
        const int left = kChoiceStart + column * kChoiceSpacing;
        if (x >= left && x < left + 40) {
          *durations[row] = kDurations[column];
          dirty = true;
          save();
          return true;
        }
      }
    }
    if (y >= kWifiTop && y < kWifiTop + 28 && x >= 115 && x < 285) {
      if (!canChangeWifi) {
        wifiNotice = "Wi-Fi busy. Try again.";
        return true;
      }
      confirming = true;
      return true;
    }
  }
  return wifiChanged;
}

void draw(M5Canvas& frame, bool canChangeWifi) {
  frame.fillScreen(TFT_WHITE);
  frame.setTextSize(2);
  frame.setTextColor(TFT_BLACK);
  label(frame, "Settings", 200, 51, true);
  if (WifiSetup::active()) {
    label(frame, "Connect your phone to:", 200, 127, true);
    label(frame, WifiSetup::accessPoint(), 200, 163, true);
    label(frame, "Password", 200, 195, true);
    label(frame, WifiSetup::accessPassword(), 200, 224, true);
    label(frame, WifiSetup::status(), 200, 270, true);
    button(frame, "Close", 144, kWifiTop, 112);
    return;
  }
  if (confirming) {
    label(frame, "Start Wi-Fi setup?", 200, 163, true);
    label(frame, "Use a phone to change Wi-Fi", 200, 193, true);
    button(frame, "No", 95, 225, 98);
    button(frame, "Yes", 207, 225, 98, true);
    return;
  }
  for (int index = 0; index < 3; ++index) {
    const int y = kSliderTop + index * kSliderSpacing;
    const int minimum = index == 0 ? 0 : kMinLight;
    const int knob = map(*sliders[index], minimum, 255, kSliderStart, kSliderEnd);
    label(frame, kSliderLabels[index], 38, y);
    frame.fillRoundRect(kSliderStart, y - 3, kSliderEnd - kSliderStart, 6, 3, TFT_LIGHTGREY);
    frame.fillCircle(knob, y, 8, TFT_NAVY);
    frame.setTextDatum(middle_right);
    frame.drawNumber((*sliders[index] * 100 + 127) / 255, 350, y);
  }
  for (int row = 0; row < 3; ++row) {
    const int y = kDurationTop + row * kDurationSpacing;
    label(frame, kDurationLabels[row], 38, y + 14);
    for (int column = 0; column < 4; ++column) {
      char caption[4];
      snprintf(caption, sizeof(caption), "%us", unsigned(kDurations[column]));
      button(frame, caption, kChoiceStart + column * kChoiceSpacing, y, 40, *durations[row] == kDurations[column]);
    }
  }
  if (canChangeWifi) button(frame, "Change Wi-Fi", 115, kWifiTop, 170);
  frame.setTextSize(1);
  label(frame, *wifiNotice ? wifiNotice : "Right button: back", 200, 366, true);
}
}
