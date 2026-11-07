#pragma once

#include <M5Unified.h>

namespace StopwatchSettings {
struct Values {
  uint8_t volume = 220;
  uint8_t avatarLight = 64;
  uint8_t badgeLight = 64;
  uint8_t avatarSeconds = 30;
  uint8_t badgeSeconds = 60;
  uint8_t dimSeconds = 60;
};

extern Values values;
enum class Update { None, Changed, Close };
void load();
void open();
void close();
Update update(int x, int y, bool pressed, bool firstPress, bool canChangeWifi);
void draw(M5Canvas& frame, bool canChangeWifi);
}
