#include "world_stream.h"
#include "world/synthesis.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <vector>

namespace {
void require(bool value) { if (!value) { std::fputs("WORLD invariant failed\n", stderr); std::abort(); } }
bool collect(void* context, const float* p, std::size_t n) {
  auto& out = *static_cast<std::vector<float>*>(context);
  out.insert(out.end(), p, p + n);
  return true;
}
bool cancel(void*, const float*, std::size_t) { return false; }
std::vector<float> reference(const std::vector<float>& input, int frames, int samples) {
#ifdef SUMI_WORLD_FP32
  // The FP32 upstream batch API also rounds cumulative phase to float. Our
  // stream deliberately retains double phase, so it is not a valid bitwise
  // oracle. Check prefix/block invariance here; audit_world_precision.py uses
  // the independent FP64 implementation as the numerical quality oracle.
  std::vector<float> full;
  require(sumi_world::synthesize_stream(input.data(), input.size(), frames,
      static_cast<int>((frames - 1) * .005 * sumi_world::kWorldSampleRate), sumi_world::kWorldFftSize, collect, &full, nullptr));
  full.resize(samples);
  return full;
#else
  std::vector<sumi_world::WorldReal> sp(frames * 513), ap(frames * 513), f0(frames);
  std::vector<sumi_world::WorldReal*> s(frames), a(frames);
  for (int i = 0; i < frames; ++i) {
    float frame[64];
    for (int c = 0; c < 64; ++c) frame[c] = input[c * frames + i];
    s[i] = sp.data() + i * 513; a[i] = ap.data() + i * 513; f0[i] = frame[62];
    require(sumi_world::decode_world_frame(frame, s[i], a[i]));
  }
  std::vector<sumi_world::WorldReal> wave(static_cast<int>(frames * 5.0 * sumi_world::kWorldSampleRate / 1000));
  Synthesis(f0.data(), frames, s.data(), a.data(), 1024, 5.0, sumi_world::kWorldSampleRate, wave.size(), wave.data());
  return std::vector<float>(wave.begin(), wave.begin() + samples);
#endif
}
}

int main() {
  for (int frames : {2, 3, 49, 65, 129}) {
    for (int mode = 0; mode < 6; ++mode) {
      std::vector<float> input(64 * frames);
      for (int i = 0; i < frames; ++i) {
        input[i] = -6.0f;
        input[frames + i] = 0.1f * std::sin(i * .3f);
        input[60 * frames + i] = mode == 0 ? -.001f : -25.0f;
        input[61 * frames + i] = mode == 0 ? -.001f : -15.0f;
        input[62 * frames + i] = mode == 0 ? 0 : mode == 1 ? 65 : mode == 2 ? 600 :
            mode == 3 ? (i % 2 ? 0 : 220) : mode == 4 ? 100 + 400.0f * i / frames : (i == frames - 1 ? 0 : 220);
        input[63 * frames + i] = input[62 * frames + i] > 0 ? 1 : 0;
      }
      const auto untouched = input;
      for (int samples : {1, static_cast<int>((frames - 1) * .005 * sumi_world::kWorldSampleRate)}) {
        const auto expected = reference(input, frames, samples);
        for (int block : {1, 7, sumi_world::kWorldFftSize / 2, sumi_world::kWorldFftSize}) {
          std::vector<float> out;
          sumi_world::SynthesisStats stats;
          require(sumi_world::synthesize_stream(input.data(), input.size(), frames, samples, block, collect, &out, &stats));
          require(out == expected && input == untouched && stats.output_samples == out.size());
          require(stats.decoded_frames <= static_cast<std::size_t>(frames));
          for (float v : out) require(std::isfinite(v));
        }
      }
    }
  }
  std::vector<float> input(64 * 3);
  input[0] = input[1] = input[2] = -6;
  std::vector<float> out;
  using sumi_world::synthesize_stream;
  require(!synthesize_stream(nullptr, input.size(), 3, 100, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size() - 1, 3, 100, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 1, 1, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 24001, 1, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 3, 221, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 3, 0, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 3, 100, 0, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 3, 100, sumi_world::kWorldFftSize + 1, collect, &out, nullptr));
  require(!synthesize_stream(input.data(), input.size(), 3, 100, sumi_world::kWorldFftSize, nullptr, &out, nullptr));
  for (int channel : {0, 59, 60, 61, 62, 63}) {
    const float before = input[channel * 3];
    input[channel * 3] = std::numeric_limits<float>::quiet_NaN();
    require(!synthesize_stream(input.data(), input.size(), 3, 100, sumi_world::kWorldFftSize, collect, &out, nullptr));
    input[channel * 3] = before;
  }
  input[62 * 3] = 601;
  require(!synthesize_stream(input.data(), input.size(), 3, 100, sumi_world::kWorldFftSize, collect, &out, nullptr));
  input[62 * 3] = 0;
  require(out.empty());
  require(!synthesize_stream(input.data(), input.size(), 3, 100, 7, cancel, nullptr, nullptr));
  require(synthesize_stream(input.data(), input.size(), 3, 100, sumi_world::kWorldFftSize, collect, &out, nullptr));
  require(out.size() == 100);
  std::puts("WORLD stream invariant checks passed");
}
