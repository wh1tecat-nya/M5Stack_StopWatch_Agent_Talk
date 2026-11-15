// Host-only comparison with the unchanged full-array WORLD implementation.
#include "world_stream.h"
#include "world/synthesis.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
bool collect(void* context, const float* data, std::size_t count) {
  auto& out = *static_cast<std::vector<float>*>(context);
  out.insert(out.end(), data, data + count);
  return true;
}
std::size_t number(const char* arg) {
  std::size_t used;
  const std::string text(arg);
  if (text.empty() || text[0] == '-') throw std::runtime_error("Invalid number");
  auto result = std::stoull(text, &used);
  if (used != text.size()) throw std::runtime_error("Invalid number");
  return result;
}
}
int main(int argc, char** argv) {
  try {
    if (argc != 6) throw std::runtime_error("Usage: physical.f32 output.f32 samples block_size repeats (block=0: full reference)");
    std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
    if (!input || input.tellg() <= 0 || input.tellg() % 256) throw std::runtime_error("Invalid features");
    const std::size_t frames = static_cast<std::size_t>(input.tellg()) / 256;
    if (frames < 2 || frames > 24000 || std::filesystem::exists(argv[2])) throw std::runtime_error("Invalid frames or existing output");
    std::vector<float> physical(frames * 64);
    input.seekg(0); input.read(reinterpret_cast<char*>(physical.data()), physical.size() * 4);
    if (!input) throw std::runtime_error("Short input");
    const auto samples = number(argv[3]), block = number(argv[4]), repeats = number(argv[5]);
#ifdef SUMI_WORLD_FFT_DSP
    if (!block) throw std::runtime_error("DSP FFT audit requires streaming mode");
#endif
    if (!samples || samples > (frames - 1) * sumi_world::kWorldSampleRate / 200 || block > 1024 || repeats < 1 || repeats > 10)
      throw std::runtime_error("Invalid dimensions");
    for (std::size_t c = 0; c < 64; ++c) for (std::size_t i = 0; i < frames; ++i) {
      const auto v = physical[c * frames + i];
      if (!std::isfinite(v) || (c < 60 && std::abs(v) > 200) || (c == 62 && (v < 0 || v > 600)) ||
          (c == 63 && (v < 0 || v > 1))) throw std::runtime_error("Invalid feature value");
    }
    double fastest = 1e30;
    std::vector<float> result;
    sumi_world::SynthesisStats stats{};
    for (std::size_t repeat = 0; repeat < repeats; ++repeat) {
      result.clear(); result.reserve(samples);
      auto start = std::chrono::steady_clock::now();
      if (block) {
        if (!sumi_world::synthesize_stream(physical.data(), physical.size(), frames, samples, block,
                                           collect, &result, &stats)) throw std::runtime_error("Streaming synthesis failed");
      } else {
#ifdef SUMI_WORLD_FFT_DSP
        throw std::runtime_error("Use the FP64 build for the independent full-reference mode");
#else
        std::vector<sumi_world::WorldReal> f0(frames), sp(frames * 513), ap(frames * 513);
        std::vector<sumi_world::WorldReal*> sp_rows(frames), ap_rows(frames);
        for (std::size_t i = 0; i < frames; ++i) {
          float frame[64];
          for (int c = 0; c < 64; ++c) frame[c] = physical[c * frames + i];
          f0[i] = frame[62]; sp_rows[i] = sp.data() + i * 513; ap_rows[i] = ap.data() + i * 513;
          if (!sumi_world::decode_world_frame(frame, sp_rows[i], ap_rows[i])) throw std::runtime_error("Decode failed");
        }
        const auto full_samples = static_cast<std::size_t>(frames * 5.0 * sumi_world::kWorldSampleRate / 1000);
        std::vector<sumi_world::WorldReal> wave(full_samples);
        Synthesis(f0.data(), static_cast<int>(frames), sp_rows.data(), ap_rows.data(), 1024, 5.0, sumi_world::kWorldSampleRate,
                  static_cast<int>(full_samples), wave.data());
        result.assign(wave.begin(), wave.begin() + samples);
#endif
      }
      fastest = std::min(fastest, std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count());
    }
    if (result.size() != samples) throw std::runtime_error("Incomplete output");
    for (float v : result) if (!std::isfinite(v)) throw std::runtime_error("Nonfinite waveform");
    std::ofstream output(argv[2], std::ios::binary);
    output.write(reinterpret_cast<const char*>(result.data()), result.size() * 4);
    if (!output) throw std::runtime_error("Write failed");
    std::cout << std::setprecision(10) << "{\"frames\":" << frames << ",\"samples\":" << samples
              << ",\"block\":" << block << ",\"fastest_seconds\":" << fastest
              << ",\"wrapper_state_bytes\":" << stats.wrapper_state_bytes
              << ",\"decoded_frames\":" << stats.decoded_frames << ",\"pulses\":" << stats.pulses << "}\n";
    return 0;
  } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
