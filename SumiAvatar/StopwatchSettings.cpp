#include "StopwatchSettings.h"
#include "WifiSetup.h"

#include <Preferences.h>

namespace StopwatchSettings {
Values values;

namespace {
enum class Page { List, Slider, Duration, WifiConfirm };
constexpr uint8_t kDurations[] = {15, 30, 45, 60};
constexpr const char* kTitles[] = {"Volume", "Avatar brightness", "Badge brightness",
                                 "Avatar timeout", "Badge lit timeout", "Badge dim timeout", "Change Wi-Fi"};
constexpr int kItemCount = 7;
constexpr int kListTop = 88;
constexpr int kListBottom = 300;
constexpr int kBackTop = 310;
constexpr int kRowHeight = 76;
constexpr int kMaxScroll = kItemCount * kRowHeight - (kListBottom - kListTop);
constexpr int kSliderStart = 68;
constexpr int kSliderEnd = 332;
constexpr int kSliderY = 226;
constexpr int kMinLight = 24;
uint8_t* const sliders[] = {&values.volume, &values.avatarLight, &values.badgeLight};
uint8_t* const durations[] = {&values.avatarSeconds, &values.badgeSeconds, &values.dimSeconds};
Page page = Page::List;
int selected = 0;
int scroll = 0;
int touchStartX = 0, touchStartY = 0, scrollStart = 0;
int lastX = 0, lastY = 0;
bool tracking = false;
bool moved = false;
bool dragging = false;
bool dirty = false;
bool wifiWasActive = false;
const char* notice = "";
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
  frame.fillRoundRect(x, y, width, 48, 12, selected ? TFT_NAVY : TFT_LIGHTGREY);
  frame.setTextColor(selected ? TFT_WHITE : TFT_BLACK);
  label(frame, text, x + width / 2, y + 24, true);
  frame.setTextColor(TFT_BLACK);
}

bool onBack(int x, int y) {
  const int dx = x - 200;
  const int dy = y - 200;
  return y >= kBackTop && dx * dx + dy * dy <= 233 * 233;
}

Update tap(int x, int y, bool canChangeWifi) {
  if (onBack(x, y)) {
    save();
    if (page == Page::List && !WifiSetup::active()) return Update::Close;
    WifiSetup::stop();
    wifiWasActive = false;
    page = Page::List;
    notice = "";
    return Update::Changed;
  }
  if (WifiSetup::active()) return Update::None;
  if (page == Page::List) {
    if (x < 48 || x >= 352 || y < kListTop || y >= kListBottom) return Update::None;
    selected = (y - kListTop + scroll) / kRowHeight;
    if (selected >= kItemCount) return Update::None;
    notice = "";
    page = selected < 3 ? Page::Slider : selected < 6 ? Page::Duration : Page::WifiConfirm;
    return Update::Changed;
  }
  if (page == Page::Duration) {
    for (int index = 0; index < 4; ++index) {
      const int left = index % 2 == 0 ? 74 : 210;
      const int top = index < 2 ? 144 : 216;
      if (x >= left && x < left + 116 && y >= top && y < top + 48) {
        *durations[selected - 3] = kDurations[index];
        dirty = true;
        save();
        return Update::Changed;
      }
    }
  }
  if (page == Page::WifiConfirm && y >= 218 && y < 266) {
    if (x >= 74 && x < 190) {
      page = Page::List;
    } else if (x >= 210 && x < 326) {
      if (!canChangeWifi) {
        notice = "Wi-Fi busy. Try again.";
      } else if (!WifiSetup::start()) {
        notice = "Could not start Wi-Fi setup";
      }
    } else {
      return Update::None;
    }
    return Update::Changed;
  }
  return Update::None;
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
  page = Page::List;
  tracking = moved = dragging = false;
  notice = "";
  previousWifiStatus = nullptr;
}

void close() {
  save();
  WifiSetup::stop();
  wifiWasActive = false;
  tracking = dragging = false;
}

Update update(int x, int y, bool pressed, bool firstPress, bool canChangeWifi) {
  WifiSetup::update();
  const bool wifiActive = WifiSetup::active();
  bool redraw = wifiActive != wifiWasActive;
  if (wifiWasActive && !wifiActive) page = Page::List;
  wifiWasActive = wifiActive;
  if (wifiActive) {
    const char* status = WifiSetup::status();
    redraw = redraw || status != previousWifiStatus;
    previousWifiStatus = status;
  }
  if (firstPress) {
    tracking = true;
    moved = false;
    touchStartX = x;
    touchStartY = y;
    scrollStart = scroll;
    dragging = !wifiActive && page == Page::Slider &&
               x >= kSliderStart - 18 && x <= kSliderEnd + 18 && abs(y - kSliderY) <= 36;
  }
  if (pressed && tracking) {
    lastX = x;
    lastY = y;
    moved = moved || abs(y - touchStartY) > 10 || abs(x - touchStartX) > 10;
    if (!wifiActive && page == Page::List && moved &&
        touchStartY >= kListTop && touchStartY < kListBottom) {
      const int nextScroll = constrain(scrollStart + touchStartY - y, 0, kMaxScroll);
      redraw = redraw || nextScroll != scroll;
      scroll = nextScroll;
    }
    if (dragging) {
      const int minimum = selected == 0 ? 0 : kMinLight;
      const uint8_t value = map(constrain(x, kSliderStart, kSliderEnd), kSliderStart, kSliderEnd, minimum, 255);
      if (*sliders[selected] != value) {
        *sliders[selected] = value;
        dirty = true;
        redraw = true;
      }
    }
  } else if (tracking) {
    tracking = false;
    save();
    const bool backGesture = onBack(touchStartX, touchStartY) && onBack(lastX, lastY);
    if (backGesture || (!moved && !dragging)) {
      const auto action = tap(lastX, lastY, canChangeWifi);
      if (action != Update::None) return action;
    }
    dragging = false;
  }
  return redraw ? Update::Changed : Update::None;
}

void draw(M5Canvas& frame, bool canChangeWifi) {
  frame.fillScreen(TFT_WHITE);
  frame.setTextSize(2);
  frame.setTextColor(TFT_BLACK);
  label(frame, page == Page::List ? "Settings" : kTitles[selected], 200, 55, true);
  if (WifiSetup::active()) {
    label(frame, "Connect your phone to:", 200, 115, true);
    label(frame, WifiSetup::accessPoint(), 200, 153, true);
    label(frame, "Password", 200, 187, true);
    label(frame, WifiSetup::accessPassword(), 200, 218, true);
    label(frame, WifiSetup::status(), 200, 268, true);
  } else if (page == Page::List) {
    frame.setClipRect(45, kListTop, 310, kListBottom - kListTop);
    for (int index = 0; index < kItemCount; ++index) {
      const int y = kListTop + index * kRowHeight - scroll;
      if (y + kRowHeight <= kListTop || y >= kListBottom) continue;
      frame.fillRoundRect(48, y + 3, 292, kRowHeight - 6, 12, TFT_LIGHTGREY);
      label(frame, kTitles[index], 62, y + 24);
      char caption[40];
      if (index < 3) {
        snprintf(caption, sizeof(caption), "%u%%", unsigned((*sliders[index] * 100 + 127) / 255));
      } else if (index < 6) {
        snprintf(caption, sizeof(caption), "%u seconds", unsigned(*durations[index - 3]));
      } else {
        strlcpy(caption, canChangeWifi ? "Phone setup" : "Available when idle", sizeof(caption));
      }
      frame.setTextColor(TFT_DARKGREY);
      label(frame, caption, 62, y + 52);
      label(frame, ">", 318, y + 38, true);
      frame.setTextColor(TFT_BLACK);
    }
    frame.clearClipRect();
    const int thumbHeight = (kListBottom - kListTop) * (kListBottom - kListTop) / (kItemCount * kRowHeight);
    const int thumbY = kListTop + scroll * (kListBottom - kListTop - thumbHeight) / kMaxScroll;
    frame.fillRoundRect(348, thumbY, 5, thumbHeight, 2, TFT_DARKGREY);
  } else if (page == Page::Slider) {
    char caption[6];
    snprintf(caption, sizeof(caption), "%u%%", unsigned((*sliders[selected] * 100 + 127) / 255));
    frame.setTextSize(5);
    label(frame, caption, 200, 150, true);
    frame.setTextSize(2);
    const int minimum = selected == 0 ? 0 : kMinLight;
    const int knob = map(*sliders[selected], minimum, 255, kSliderStart, kSliderEnd);
    frame.fillRoundRect(kSliderStart, kSliderY - 5, kSliderEnd - kSliderStart, 10, 5, TFT_LIGHTGREY);
    frame.fillRoundRect(kSliderStart, kSliderY - 5, knob - kSliderStart, 10, 5, TFT_NAVY);
    frame.fillCircle(knob, kSliderY, 17, TFT_NAVY);
    label(frame, "Drag to adjust", 200, 279, true);
  } else if (page == Page::Duration) {
    for (int index = 0; index < 4; ++index) {
      char caption[12];
      snprintf(caption, sizeof(caption), "%u sec", unsigned(kDurations[index]));
      button(frame, caption, index % 2 == 0 ? 74 : 210, index < 2 ? 144 : 216, 116,
             *durations[selected - 3] == kDurations[index]);
    }
  } else {
    label(frame, "Start phone Wi-Fi setup?", 200, 151, true);
    button(frame, "No", 74, 218, 116);
    button(frame, "Yes", 210, 218, 116, true);
    if (*notice) {
      frame.setTextSize(1);
      label(frame, notice, 200, 293, true);
      frame.setTextSize(2);
    }
  }
}

void drawBack(M5Canvas& screen) {
  const int centerX = screen.width() / 2;
  const int centerY = screen.height() / 2;
  const int top = (screen.height() - 400) / 2 + kBackTop;
  screen.setClipRect(0, top, screen.width(), screen.height() - top);
  screen.fillCircle(centerX, centerY, screen.height() / 2 - 1, TFT_LIGHTGREY);
  screen.drawCircle(centerX, centerY, 174, TFT_WHITE);
  screen.clearClipRect();
  const int arrowY = top + 33;
  screen.drawLine(centerX - 17, arrowY, centerX + 15, arrowY, TFT_DARKGREY);
  screen.drawLine(centerX - 17, arrowY, centerX - 7, arrowY - 10, TFT_DARKGREY);
  screen.drawLine(centerX - 17, arrowY, centerX - 7, arrowY + 10, TFT_DARKGREY);
  screen.setFont(&fonts::lgfxJapanGothic_20);
  screen.setTextSize(1);
  screen.setTextColor(TFT_DARKGREY);
  screen.setTextDatum(middle_center);
  screen.drawString("戻る", centerX, arrowY + 37);
  screen.setFont(&fonts::Font0);
}
}
