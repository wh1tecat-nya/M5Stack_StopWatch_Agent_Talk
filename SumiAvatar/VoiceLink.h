#pragma once

#include <Arduino.h>

namespace VoiceLink {
constexpr size_t kRecordRate = 16000;
constexpr size_t kPlaybackRate = 24000;
constexpr size_t kRecordSamples = 15 * kRecordRate;
constexpr size_t kPlaybackSamples = 30 * kPlaybackRate;

enum class Job : uint8_t { Upload, Poll, Acknowledge };

struct Request {
  Job job;
  size_t samples;
  char id[33];
};

struct Result {
  Request request;
  int status;
  size_t samples;
};

bool begin(int16_t* recording, int16_t* playback);
bool connected();
bool send(const Request& request);
bool receive(Result& result);
void newId(char (&id)[33]);
}
