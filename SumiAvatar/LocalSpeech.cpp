#include "LocalSpeech.h"

#ifdef SUMI_SANOTTS
#include <SanoTTS.h>
#include <cstring>
#endif

namespace LocalSpeech {
#ifdef SUMI_SANOTTS
namespace {
struct Output {
  int16_t* pcm;
  size_t capacity;
  size_t samples;
  bool overflow;
};

void append(const int16_t* chunk, size_t samples, void* user) {
  auto& output = *static_cast<Output*>(user);
  if (output.overflow || samples > output.capacity - output.samples) {
    output.overflow = true;
    return;
  }
  std::memcpy(output.pcm + output.samples, chunk, samples * sizeof(int16_t));
  output.samples += samples;
}
}

size_t synthesize(const char* text, int16_t* pcm, size_t capacity) {
  static SanoTTS voice;
  static_assert(SanoTTS::kSampleRate == kSampleRate);
  if ((!voice.ready() && !voice.begin()) || !voice.kanjiReady()) return 0;
  Output output{pcm, capacity, 0, false};
  if (!voice.synthesize(text, append, &output) || output.overflow) return 0;
  return output.samples;
}
#else
size_t synthesize(const char*, int16_t*, size_t) { return 0; }
#endif
}
