#include "world_stream.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <vector>

namespace {
void require(bool ok) { if (!ok) std::abort(); }
bool collect(void* context, const float* data, std::size_t size) {
  auto& out = *static_cast<std::vector<float>*>(context);
  out.insert(out.end(), data, data + size);
  return true;
}
struct Producer {
  const std::vector<float>& source;
  std::vector<float>& target;
  std::size_t frames, step, ready = 0, calls = 0, cancel_at = 0;
  bool corrupt = false;
  static bool wait(void* context, std::size_t needed) {
    auto& self = *static_cast<Producer*>(context);
    require(needed <= self.frames);
    ++self.calls;
    if (self.cancel_at && needed >= self.cancel_at) return false;
    const auto end = std::min(self.frames, (needed + self.step - 1) / self.step * self.step);
    for (std::size_t c = 0; c < 64; ++c) for (std::size_t t = self.ready; t < end; ++t)
      self.target[c * self.frames + t] = self.source[c * self.frames + t];
    self.ready = std::max(self.ready, end);
    if (self.corrupt && needed > 30) self.target[62 * self.frames + needed - 1] = 601;
    return true;
  }
};
}

int main() {
  constexpr std::size_t frames = 129, samples = (frames - 1) * sumi_world::kWorldSampleRate / 200;
  std::vector<float> source(frames * 64, 0.f), expected;
  for (std::size_t i = 0; i < frames; ++i) {
    source[i] = -6.f;
    source[frames + i] = .1f * std::sin(i * .3f);
    source[60 * frames + i] = -25.f;
    source[61 * frames + i] = -15.f;
    source[62 * frames + i] = (i / 20) % 2 ? 0.f : 150.f + i;
    source[63 * frames + i] = source[62 * frames + i] > 0 ? 1.f : 0.f;
  }
  require(sumi_world::synthesize_stream(source.data(), source.size(), frames, samples,
      sumi_world::kWorldFftSize, collect, &expected, nullptr));
  for (std::size_t step : {1, 7, 32, 64, 129}) for (std::size_t block : {1, 7, 256, 512}) {
    if (block > sumi_world::kWorldFftSize) continue;
    std::vector<float> pending(source.size(), std::numeric_limits<float>::quiet_NaN()), actual;
    Producer producer{source, pending, frames, step};
    require(sumi_world::synthesize_stream(pending.data(), pending.size(), frames, samples,
        block, collect, &actual, nullptr, nullptr, 0, Producer::wait, &producer));
    require(actual == expected && producer.calls > 0);
  }
  for (bool corrupt : {false, true}) {
    std::vector<float> pending(source.size(), std::numeric_limits<float>::quiet_NaN()), partial;
    Producer producer{source, pending, frames, 1};
    producer.cancel_at = corrupt ? 0 : 31;
    producer.corrupt = corrupt;
    require(!sumi_world::synthesize_stream(pending.data(), pending.size(), frames, samples,
        7, collect, &partial, nullptr, nullptr, 0, Producer::wait, &producer));
    require(partial.size() < expected.size());
    require(std::equal(partial.begin(), partial.end(), expected.begin()));
  }
  std::puts("Incremental feature checks passed");
}
