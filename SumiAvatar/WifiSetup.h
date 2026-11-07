#pragma once

#include <Arduino.h>

namespace WifiSetup {
void connect(const char* defaultSsid, const char* defaultPassword);
bool start();
void stop();
void update();
bool active();
bool connected();
const char* accessPoint();
const char* accessPassword();
const char* status();
}
