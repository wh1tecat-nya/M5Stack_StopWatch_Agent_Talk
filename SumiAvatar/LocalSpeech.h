#pragma once

#include <stddef.h>
#include <stdint.h>

namespace LocalSpeech {
constexpr uint32_t kSampleRate = 22050;
size_t synthesize(const char* text, int16_t* pcm, size_t capacity);
}
