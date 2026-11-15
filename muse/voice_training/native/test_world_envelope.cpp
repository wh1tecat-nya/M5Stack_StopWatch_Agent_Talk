#include "world_envelope.h"

#include <algorithm>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

void require(bool value) { if (!value) throw std::runtime_error("Native invariant failed"); }

int main() {
  using sumi_world::EnvelopePredictor;
  try {
    std::vector<float> weights(sumi_world::kWeightFloats, 0.f);
    std::fill(weights.begin() + 32, weights.begin() + 64, 1.f);
    std::fill(weights.begin() + 126, weights.begin() + 188, 1.f);
    weights[188] = std::log(220.f); weights[189] = .2f;
    std::fill(weights.end() - 60, weights.end(), .125f);
    EnvelopePredictor model;
    require(!model.bind(weights.data(), weights.size() - 1));
    require(model.bind(weights.data(), weights.size()));
    alignas(16) float matrix[sumi_world::kMatrixWorkspaceFloats + 4];
    for (std::size_t frames : {1u, 3u, 164u}) {
      const auto size = EnvelopePredictor::workspace_floats(frames);
      std::vector<float> input(32 * frames, 0.f), out(64 * frames + 8, 99.f), scratch(size + 8, 99.f);
      require(!model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                             scratch.data() + 4, size - 1));
      require(std::all_of(out.begin(), out.end(), [](float x) { return x == 99.f; }));
      require(model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                            scratch.data() + 4, size));
      for (std::size_t i = 0; i < 64 * frames; ++i) require(out[i + 4] == (i < 60 * frames ? .125f : 0.f));
      require(!model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                            scratch.data() + 4, size, matrix, sumi_world::kMatrixWorkspaceFloats - 1));
      require(!model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                            scratch.data() + 4, size, matrix + 1, sumi_world::kMatrixWorkspaceFloats));
      require(!model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                            scratch.data() + 4, size, nullptr, 1));
      require(model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                            scratch.data() + 4, size, matrix, sumi_world::kMatrixWorkspaceFloats));
      for (std::size_t i = 0; i < 64 * frames; ++i) require(out[i + 4] == (i < 60 * frames ? .125f : 0.f));
      for (std::size_t i = 0; i < 4; ++i) {
        require(out[i] == 99.f && out[out.size() - 1 - i] == 99.f);
        require(scratch[i] == 99.f && scratch[scratch.size() - 1 - i] == 99.f);
      }
      require(!model.predict(scratch.data(), input.size(), frames, out.data() + 4, 64 * frames,
                             scratch.data() + 4, size));
      require(model.to_physical(out.data() + 4, 64 * frames, frames));
      require(std::abs(out[4 + 62 * frames] - 220.f) < .001f);
      require(out[4 + 63 * frames] == .5f);
      input[0] = std::numeric_limits<float>::infinity();
      require(!model.predict(input.data(), input.size(), frames, out.data() + 4, 64 * frames,
                             scratch.data() + 4, size));
    }
    require(EnvelopePredictor::workspace_floats(0) == 0);
    require(EnvelopePredictor::workspace_floats(sumi_world::kMaxWindowFrames + 1) == 0);
    weights[32] = 0.f;
    require(!model.bind(weights.data(), weights.size()));
    std::cout << "Native invariant checks passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n'; return 1;
  }
}
