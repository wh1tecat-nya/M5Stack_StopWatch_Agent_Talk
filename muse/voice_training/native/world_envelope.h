#pragma once

#include <cstddef>

namespace sumi_world {

// sumi_world_envelope_fp32_v1 の固定構造。重みは little-endian IEEE754 FP32。
// 呼び出し元で model.json の構造と SHA-256 を照合してから bind する。
constexpr std::size_t kWeightFloats = 114826;
constexpr std::size_t kInputChannels = 32;
constexpr std::size_t kOutputChannels = 64;
constexpr std::size_t kHaloFrames = 50;
constexpr std::size_t kMaxWindowFrames = 65536;
#ifdef SUMI_WORLD_MATRIX_TILE_FRAMES
constexpr std::size_t kMatrixTileFrames = SUMI_WORLD_MATRIX_TILE_FRAMES;
static_assert(kMatrixTileFrames == 4 || kMatrixTileFrames == 8);
#elif defined(SUMI_WORLD_COMPACT_WORKSPACE)
constexpr std::size_t kMatrixTileFrames = 4;
#else
constexpr std::size_t kMatrixTileFrames = 8;
#endif
constexpr std::size_t kMatrixOutputRows = 4;
constexpr std::size_t kMatrixWorkspaceFloats = (288 + kMatrixOutputRows) * kMatrixTileFrames + 288 * kMatrixOutputRows;

class EnvelopePredictor {
 public:
  struct Profile { unsigned long long dense_us = 0, depth_us = 0, activation_us = 0, dense_macs = 0, pack_us = 0, matmul_us = 0, input_us = 0, weights_us = 0; };
  Profile profile() const { return profile_; }
  // 重みの所有権は呼び出し元に残る。モデル利用中は変更・解放しない。
  bool bind(const float* weights, std::size_t count);
  static std::size_t workspace_floats(std::size_t frames);

  // 入出力は channel-major。workspace を含む各領域は重ねない。
  // 推論中の動的メモリー確保は行わない。失敗時の出力は使用しない。
  bool predict(const float* input, std::size_t input_count, std::size_t frames,
               float* output, std::size_t output_count,
               float* workspace, std::size_t workspace_count,
               float* matrix_workspace = nullptr, std::size_t matrix_count = 0) const;

  // 正規化出力を同じ領域で 62特徴・有声F0・有声確率に変換する。
  bool to_physical(float* output, std::size_t count, std::size_t frames) const;

 private:
  struct Conv {
    const float* weight = nullptr;
    const float* bias = nullptr;
    int inputs = 0, outputs = 0, kernel = 0, dilation = 1;
    bool depthwise = false;
  };
  struct Block { Conv depth, expand, reduce; };
  static void convolve(const Conv& layer, const float* input, float* output,
                       std::size_t frames, float* matrix_workspace, Profile& profile);
  static void activate(float* values, std::size_t count, Profile& profile);
  static void block(const Block& layer, float* state, float* temporary,
                    float* expanded, std::size_t frames, float* matrix_workspace, Profile& profile);

  const float* weights_ = nullptr;
  const float *z_mean_ = nullptr, *z_std_ = nullptr;
  const float *y_mean_ = nullptr, *y_std_ = nullptr;
  const float *f0_mean_ = nullptr, *f0_std_ = nullptr;
  Conv base_input_, base_output_, refine_input_, refine_output_;
  Block base_blocks_[4], refine_blocks_[2];
  mutable Profile profile_{};
};

}  // namespace sumi_world
