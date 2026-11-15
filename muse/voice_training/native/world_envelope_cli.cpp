// ホスト照合専用。ファイル・動的配列・計時処理はファームウェアへ組み込まない。
#include "world_envelope.h"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
std::vector<float> read(const std::string& path) {
  std::ifstream f(path, std::ios::binary | std::ios::ate);
  if (!f || f.tellg() <= 0 || f.tellg() % 4) throw std::runtime_error("Invalid input file");
  std::vector<float> result(static_cast<std::size_t>(f.tellg()) / 4);
  f.seekg(0); f.read(reinterpret_cast<char*>(result.data()), static_cast<std::streamsize>(result.size() * 4));
  if (!f) throw std::runtime_error("Short input file");
  return result;
}
void write(const std::string& path, const std::vector<float>& values) {
  if (std::filesystem::exists(path)) throw std::runtime_error("Output already exists");
  std::ofstream f(path, std::ios::binary);
  f.write(reinterpret_cast<const char*>(values.data()), static_cast<std::streamsize>(values.size() * 4));
  if (!f) throw std::runtime_error("Output write failed");
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 6 && argc != 7) throw std::runtime_error("Usage: weights.f32 input.f32 output.f32 chunk_frames repeats [tiled]");
    const bool tiled = argc == 7 && std::string(argv[6]) == "tiled";
    if (argc == 7 && !tiled) throw std::runtime_error("Invalid tile mode");
    alignas(16) float tile_workspace[sumi_world::kMatrixWorkspaceFloats];
    const auto weights = read(argv[1]);
    const auto input = read(argv[2]);
    if (input.size() % 32) throw std::runtime_error("Invalid input shape");
    const std::size_t frames = input.size() / 32;
    std::size_t consumed = 0;
    const int chunk_arg = std::stoi(argv[4], &consumed);
    if (consumed != std::string(argv[4]).size() || chunk_arg < 0 || chunk_arg > 8192)
      throw std::runtime_error("Invalid chunk size");
    const int repeats = std::stoi(argv[5], &consumed);
    if (consumed != std::string(argv[5]).size() || repeats < 1 || repeats > 10)
      throw std::runtime_error("Invalid repeat count");
    const std::size_t chunk = chunk_arg ? static_cast<std::size_t>(chunk_arg) : frames;
    const auto maximum_window = std::min(frames, chunk + 2 * sumi_world::kHaloFrames);
    const auto scratch_size = sumi_world::EnvelopePredictor::workspace_floats(maximum_window);
    if (!scratch_size || std::filesystem::exists(argv[3]) ||
        std::filesystem::exists(std::string(argv[3]) + ".physical.f32"))
      throw std::runtime_error("Invalid window or existing output");
    sumi_world::EnvelopePredictor model;
    if (!model.bind(weights.data(), weights.size())) throw std::runtime_error("Invalid weights");
    std::vector<float> workspace(scratch_size), local_input(32 * maximum_window);
    std::vector<float> local_output(64 * maximum_window), output(64 * frames);
    double fastest = std::numeric_limits<double>::max();
    for (int repeat = 0; repeat < repeats; ++repeat) {
      const auto begin = std::chrono::steady_clock::now();
      for (std::size_t start = 0; start < frames; start += chunk) {
        const auto stop = std::min(start + chunk, frames);
        const auto left = start > sumi_world::kHaloFrames ? start - sumi_world::kHaloFrames : 0;
        const auto right = std::min(frames, stop + sumi_world::kHaloFrames);
        const auto width = right - left;
        for (std::size_t c = 0; c < 32; ++c)
          std::copy(input.data() + c * frames + left, input.data() + c * frames + right,
                    local_input.data() + c * width);
        if (!model.predict(local_input.data(), 32 * width, width, local_output.data(), 64 * width,
                           workspace.data(), workspace.size(), tiled ? tile_workspace : nullptr,
                           tiled ? sumi_world::kMatrixWorkspaceFloats : 0)) throw std::runtime_error("Prediction failed");
        for (std::size_t c = 0; c < 64; ++c)
          std::copy(local_output.data() + c * width + start - left,
                    local_output.data() + c * width + stop - left, output.data() + c * frames + start);
      }
      fastest = std::min(fastest, std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count());
    }
    write(argv[3], output);
    if (!model.to_physical(output.data(), output.size(), frames)) throw std::runtime_error("Physical conversion failed");
    write(std::string(argv[3]) + ".physical.f32", output);
    std::cout << std::setprecision(10) << "{\"frames\":" << frames
              << ",\"window_frames\":" << maximum_window << ",\"chunk_frames\":" << chunk
              << ",\"repeats\":" << repeats << ",\"fastest_seconds\":" << fastest
              << ",\"workspace_bytes\":" << workspace.size() * 4
              << ",\"window_io_bytes\":" << (local_input.size() + local_output.size()) * 4
              << ",\"model_state_bytes\":" << sizeof(model)
              << ",\"weight_bytes\":" << weights.size() * 4 << "}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
