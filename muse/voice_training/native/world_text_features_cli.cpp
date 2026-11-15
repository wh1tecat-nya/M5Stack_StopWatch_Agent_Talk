// Export the existing Japanese text encoder without running its waveform decoder.
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>
extern "C" {
#include "saanotts_internal.h"
saan_status sumi_text_acoustic(const saan_weights*, saan_arena*, const int32_t*, int,
                              const int32_t*, int, float*);
}
namespace {
template<class T> std::vector<T> read(const char* path, std::size_t maximum) {
  std::ifstream in(path, std::ios::binary | std::ios::ate);
  const auto bytes = in.tellg();
  if (!in || bytes <= 0 || bytes > static_cast<std::streamoff>(maximum * sizeof(T)) || bytes % sizeof(T))
    throw std::runtime_error("Invalid input size");
  std::vector<T> result(static_cast<std::size_t>(bytes) / sizeof(T));
  in.seekg(0);
  if (!in.read(reinterpret_cast<char*>(result.data()), bytes)) throw std::runtime_error("Cannot read input");
  return result;
}
void write(const std::filesystem::path& path, const std::vector<float>& values) {
  for (auto value : values) if (!std::isfinite(value)) throw std::runtime_error("Non-finite encoder output");
  std::ofstream out(path, std::ios::binary);
  if (!out.write(reinterpret_cast<const char*>(values.data()), values.size() * sizeof(float)))
    throw std::runtime_error("Cannot write output");
}
}
int main(int argc, char** argv) {
  try {
    if (argc != 4 && argc != 5) throw std::runtime_error("usage: world_text_features_cli model.bin ids.i32 output_directory [duration.i32]");
    const auto blob = read<uint8_t>(argv[1], 8 * 1024 * 1024);
    const auto ids = read<int32_t>(argv[2], 2048);
    const std::filesystem::path output(argv[3]);
    if (std::filesystem::exists(output)) throw std::runtime_error("Output already exists");
    for (auto id : ids) if (id < 0 || id >= SAAN_VOCAB) throw std::runtime_error("Invalid phoneme ID");
    saan_weights weights{};
    if (saan_weights_open(&weights, blob.data(), blob.size()) != SAAN_OK)
      throw std::runtime_error("Cannot open encoder weights");
    std::vector<uint8_t> scratch(4 * 1024 * 1024);
    std::vector<float> tokens(ids.size() * SAAN_AC_W), duration(ids.size());
    saan_arena arena{};
    saan_arena_init(&arena, scratch.data(), scratch.size());
    if (saan_run_duration(&weights, &arena, ids.data(), ids.size(), duration.data()) != SAAN_OK)
      throw std::runtime_error("Duration inference failed");
    const auto duration_peak = arena.peak;
    saan_arena_reset(&arena);
    if (saan_run_acoustic_tokens(&weights, &arena, ids.data(), ids.size(), tokens.data()) != SAAN_OK)
      throw std::runtime_error("Token encoder failed");
    const auto token_peak = arena.peak;
    std::vector<float> acoustic;
    std::size_t acoustic_peak = 0;
    if (argc == 5) {
      auto durations = read<int32_t>(argv[4], 2048);
      if (durations.size() != ids.size()) throw std::runtime_error("Duration count mismatch");
      int frames = 0;
      for (auto d : durations) {
        if (d < 1 || d > 80) throw std::runtime_error("Duration outside model limits");
        frames += d;
      }
      if (frames > 6000) throw std::runtime_error("Too many acoustic frames");
      acoustic.resize(static_cast<std::size_t>(frames) * SAAN_CDIM);
      saan_arena_reset(&arena);
      if (sumi_text_acoustic(&weights, &arena, ids.data(), ids.size(), durations.data(), frames,
                             acoustic.data()) != SAAN_OK) throw std::runtime_error("Acoustic encoder failed");
      acoustic_peak = arena.peak;
    }
    std::filesystem::create_directories(output);
    write(output / "tokens.f32", tokens);
    write(output / "log_duration.f32", duration);
    if (!acoustic.empty()) write(output / "acoustic.f32", acoustic);
    std::cout << "{\"tokens\":" << ids.size() << ",\"channels\":" << SAAN_AC_W
              << ",\"duration_peak_bytes\":" << duration_peak
              << ",\"encoder_peak_bytes\":" << token_peak
              << ",\"acoustic_frames\":" << acoustic.size() / SAAN_CDIM
              << ",\"acoustic_peak_bytes\":" << acoustic_peak << "}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
