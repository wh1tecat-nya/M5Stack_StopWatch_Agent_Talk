#include "world_envelope.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#ifdef SUMI_WORLD_SILU_TABLE
#include "world_activation.h"
#endif
#ifdef ESP_PLATFORM
#include "esp_timer.h"
#endif
#ifdef SUMI_WORLD_ESP_DSP
#include "dspm_mult.h"
#endif

namespace sumi_world {
namespace {
struct Timed {
  unsigned long long& counter;
#ifdef ESP_PLATFORM
  const int64_t begin = esp_timer_get_time();
  ~Timed() { counter += esp_timer_get_time() - begin; }
#endif
};
void copy_floats(const float* source, float* destination, int count) {
  int i = 0;
  for (; i + 4 <= count; i += 4) {
    const float a = source[i], b = source[i + 1], c = source[i + 2], d = source[i + 3];
    destination[i] = a; destination[i + 1] = b; destination[i + 2] = c; destination[i + 3] = d;
  }
  for (; i < count; ++i) destination[i] = source[i];
}
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559,
              "IEEE754 FP32 is required");

bool valid_span(const float* p, std::size_t count) {
  const auto address = reinterpret_cast<std::uintptr_t>(p);
  return p && address % alignof(float) == 0 && count > 0 &&
         count <= (std::numeric_limits<std::uintptr_t>::max() - address) / sizeof(float);
}

bool overlap(const float* a, std::size_t na, const float* b, std::size_t nb) {
  const auto aa = reinterpret_cast<std::uintptr_t>(a);
  const auto bb = reinterpret_cast<std::uintptr_t>(b);
  return aa < bb + nb * sizeof(float) && bb < aa + na * sizeof(float);
}

bool finite(const float* x, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) if (!std::isfinite(x[i])) return false;
  return true;
}
}  // namespace

bool EnvelopePredictor::bind(const float* weights, std::size_t count) {
  weights_ = nullptr;
  const std::uint32_t endian = 1;
  if (*reinterpret_cast<const unsigned char*>(&endian) != 1 || count != kWeightFloats ||
      !valid_span(weights, count) || !finite(weights, count)) return false;
  std::size_t offset = 0;
  auto take = [&](std::size_t n) { const float* p = weights + offset; offset += n; return p; };
  z_mean_ = take(32); z_std_ = take(32);
  y_mean_ = take(62); y_std_ = take(62);
  f0_mean_ = take(1); f0_std_ = take(1);
  for (int i = 0; i < 32; ++i) if (!(z_std_[i] > 0)) return false;
  for (int i = 0; i < 62; ++i) if (!(y_std_[i] > 0)) return false;
  if (!(*f0_std_ > 0)) return false;
  auto conv = [&](int inputs, int outputs, int kernel, int dilation = 1, bool depth = false) {
    Conv c;
    c.inputs = inputs; c.outputs = outputs; c.kernel = kernel;
    c.dilation = dilation; c.depthwise = depth;
    c.weight = take(static_cast<std::size_t>(outputs) * (depth ? 1 : inputs) * kernel);
    c.bias = take(outputs);
    return c;
  };
  base_input_ = conv(32, 64, 3);
  for (int i = 0; i < 4; ++i) {
    auto& b = base_blocks_[i];
    b.depth = conv(64, 64, 7, 1 << i, true);
    b.expand = conv(64, 128, 1); b.reduce = conv(128, 64, 1);
  }
  base_output_ = conv(64, 64, 1);
  refine_input_ = conv(96, 48, 3);
  for (auto& b : refine_blocks_) {
    b.depth = conv(48, 48, 3, 1, true);
    b.expand = conv(48, 96, 1); b.reduce = conv(96, 48, 1);
  }
  refine_output_ = conv(48, 60, 1);
  if (offset != count) return false;
  weights_ = weights;
  return true;
}

std::size_t EnvelopePredictor::workspace_floats(std::size_t frames) {
  // 正規化入力32＋元予測64、状態64、一時64、拡張128を再利用する。
  return frames > 0 && frames <= kMaxWindowFrames ? 352 * frames : 0;
}

void EnvelopePredictor::convolve(const Conv& c, const float* input, float* output,
                                 std::size_t frames, float* matrix_workspace, Profile& profile) {
  Timed scope{c.depthwise ? profile.depth_us : profile.dense_us};
  (void)scope;
  if (!c.depthwise) profile.dense_macs += static_cast<unsigned long long>(c.inputs) * c.outputs * c.kernel * frames;
  if (!c.depthwise && matrix_workspace) {
    const int columns = c.inputs * c.kernel;
    const int pad = c.kernel / 2 * c.dilation;
    // Reuse the same SRAM budget, widening time tiles for narrower layers.
    // All row strides remain multiples of four for the ESP-DSP kernel.
    const std::size_t tile = std::min<std::size_t>(32,
        (kMatrixWorkspaceFloats - columns * kMatrixOutputRows) /
        (columns + kMatrixOutputRows) / 4 * 4);
    float* packed = matrix_workspace;
    float* local_weights = packed + columns * tile;
    float* product = local_weights + columns * kMatrixOutputRows;
    for (std::size_t start = 0; start < frames; start += tile) {
      {
        Timed scope{profile.pack_us}; (void)scope;
        Timed input_scope{profile.input_us}; (void)input_scope;
        for (int i = 0; i < c.inputs; ++i) for (int k = 0; k < c.kernel; ++k) {
          float* row = packed + (i * c.kernel + k) * tile;
          for (std::size_t t = 0; t < tile; ++t) {
            const auto index = static_cast<std::ptrdiff_t>(start + t) + k * c.dilation - pad;
            row[t] = index >= 0 && index < static_cast<std::ptrdiff_t>(frames) ? input[i * frames + index] : 0.f;
          }
        }
      }
      for (int first_output = 0; first_output < c.outputs; first_output += kMatrixOutputRows) {
        const int rows = std::min(static_cast<int>(kMatrixOutputRows), c.outputs - first_output);
        {
          Timed scope{profile.pack_us}; (void)scope;
          Timed weight_scope{profile.weights_us}; (void)weight_scope;
          copy_floats(c.weight + first_output * columns, local_weights, rows * columns);
        }
        {
          Timed scope{profile.matmul_us}; (void)scope;
#ifdef SUMI_WORLD_ESP_DSP
          dspm_mult_f32(local_weights, packed, product, rows, columns, tile);
#else
          for (int o = 0; o < rows; ++o) for (std::size_t t = 0; t < tile; ++t) {
            float sum = 0;
            for (int i = 0; i < columns; ++i) sum += local_weights[o * columns + i] * packed[i * tile + t];
            product[o * tile + t] = sum;
          }
#endif
        }
        for (int o = 0; o < rows; ++o)
          for (std::size_t t = 0; t < std::min(tile, frames - start); ++t)
            output[(first_output + o) * frames + start + t] = product[o * tile + t] + c.bias[first_output + o];
      }
    }
    return;
  }
  if (c.kernel == 1 && !c.depthwise) {
#ifdef SUMI_WORLD_ESP_DSP
    // A[outputs, inputs] * B[inputs, frames]. The S3 kernel preserves the
    // reduction order but uses fused MAC; compare its numerical error separately.
    dspm_mult_f32(c.weight, input, output, c.outputs, c.inputs, static_cast<int>(frames));
    for (int o = 0; o < c.outputs; ++o)
      for (std::size_t t = 0; t < frames; ++t)
        output[static_cast<std::size_t>(o) * frames + t] += c.bias[o];
    return;
#endif
    // Preserve the input-channel accumulation order for every output sample,
    // while walking contiguous time samples. The original channel-strided
    // inner loop repeatedly missed the ESP32-S3 external-memory cache.
    for (int o = 0; o < c.outputs; ++o) {
      float* dst = output + static_cast<std::size_t>(o) * frames;
      std::fill(dst, dst + frames, 0.0f);
      for (int i = 0; i < c.inputs; ++i) {
        const float* row = input + static_cast<std::size_t>(i) * frames;
        const float weight = c.weight[o * c.inputs + i];
        for (std::size_t t = 0; t < frames; ++t) dst[t] += row[t] * weight;
      }
      for (std::size_t t = 0; t < frames; ++t) dst[t] += c.bias[o];
    }
    return;
  }
  const int pad = c.kernel / 2 * c.dilation;
  if (!c.depthwise) {
    // Keep each sample's i,k accumulation order, but traverse contiguous time
    // samples. This also avoids large channel strides in external PSRAM.
    for (int o = 0; o < c.outputs; ++o) {
      float* dst = output + static_cast<std::size_t>(o) * frames;
      std::fill(dst, dst + frames, 0.f);
      for (int i = 0; i < c.inputs; ++i) {
        const float* row = input + static_cast<std::size_t>(i) * frames;
        for (int k = 0; k < c.kernel; ++k) {
          const int shift = k * c.dilation - pad;
          const auto first = static_cast<std::size_t>(std::max(0, -shift));
          const auto end = static_cast<std::size_t>(std::max(0, std::min(static_cast<int>(frames), static_cast<int>(frames) - shift)));
          const float weight = c.weight[(o * c.inputs + i) * c.kernel + k];
          for (std::size_t t = first; t < end; ++t)
            dst[t] += row[static_cast<std::ptrdiff_t>(t) + shift] * weight;
        }
      }
      for (std::size_t t = 0; t < frames; ++t) dst[t] += c.bias[o];
    }
    return;
  }
  for (int o = 0; o < c.outputs; ++o) {
    for (std::size_t t = 0; t < frames; ++t) {
      float sum = 0;
      const int channels = c.depthwise ? 1 : c.inputs;
      for (int i = 0; i < channels; ++i) {
        const float* row = input + static_cast<std::size_t>(c.depthwise ? o : i) * frames;
        const float* w = c.weight + (o * channels + i) * c.kernel;
        for (int k = 0; k < c.kernel; ++k) {
          const auto index = static_cast<std::ptrdiff_t>(t) + k * c.dilation - pad;
          if (index >= 0 && index < static_cast<std::ptrdiff_t>(frames)) sum += row[index] * w[k];
        }
      }
      output[static_cast<std::size_t>(o) * frames + t] = sum + c.bias[o];
    }
  }
}

void EnvelopePredictor::activate(float* values, std::size_t count, Profile& profile) {
  Timed scope{profile.activation_us}; (void)scope;
  for (std::size_t i = 0; i < count; ++i) {
#ifdef SUMI_WORLD_SILU_TABLE
    values[i] = sumi_silu_table(values[i]);
#else
    values[i] = values[i] / (1.0f + std::exp(-values[i]));
#endif
  }
}

void EnvelopePredictor::block(const Block& b, float* state, float* temporary,
                              float* expanded, std::size_t frames, float* matrix_workspace, Profile& profile) {
  convolve(b.depth, state, temporary, frames, matrix_workspace, profile);
  convolve(b.expand, temporary, expanded, frames, matrix_workspace, profile);
  activate(expanded, static_cast<std::size_t>(b.expand.outputs) * frames, profile);
  convolve(b.reduce, expanded, temporary, frames, matrix_workspace, profile);
  for (std::size_t i = 0; i < static_cast<std::size_t>(b.reduce.outputs) * frames; ++i)
    state[i] += .2f * temporary[i];
}

bool EnvelopePredictor::predict(const float* input, std::size_t input_count, std::size_t frames,
                                float* output, std::size_t output_count,
                                float* workspace, std::size_t workspace_count,
                                float* matrix_workspace, std::size_t matrix_count) const {
  const auto required = workspace_floats(frames);
  if (!weights_ || !required || input_count != 32 * frames || output_count != 64 * frames ||
      workspace_count < required || !valid_span(input, input_count) ||
      !valid_span(output, output_count) || !valid_span(workspace, required)) return false;
  if (overlap(input, input_count, output, output_count) ||
      overlap(input, input_count, workspace, required) ||
      overlap(output, output_count, workspace, required) ||
      overlap(weights_, kWeightFloats, input, input_count) ||
      overlap(weights_, kWeightFloats, output, output_count) ||
      overlap(weights_, kWeightFloats, workspace, required) || !finite(input, input_count)) return false;
  if (matrix_workspace && (matrix_count < kMatrixWorkspaceFloats ||
      !valid_span(matrix_workspace, kMatrixWorkspaceFloats) ||
      reinterpret_cast<std::uintptr_t>(matrix_workspace) % 16 != 0 ||
      overlap(matrix_workspace, kMatrixWorkspaceFloats, input, input_count) ||
      overlap(matrix_workspace, kMatrixWorkspaceFloats, output, output_count) ||
      overlap(matrix_workspace, kMatrixWorkspaceFloats, workspace, required) ||
      overlap(matrix_workspace, kMatrixWorkspaceFloats, weights_, kWeightFloats))) return false;
  if (!matrix_workspace && matrix_count) return false;
  float* combined = workspace;
  float* state = combined + 96 * frames;
  float* temporary = state + 64 * frames;
  float* expanded = temporary + 64 * frames;
  for (std::size_t c = 0; c < 32; ++c)
    for (std::size_t t = 0; t < frames; ++t)
      combined[c * frames + t] = (input[c * frames + t] - z_mean_[c]) / z_std_[c];
  convolve(base_input_, combined, state, frames, matrix_workspace, profile_);
  activate(state, 64 * frames, profile_);
  for (const auto& b : base_blocks_) block(b, state, temporary, expanded, frames, matrix_workspace, profile_);
  float* base = combined + 32 * frames;
  convolve(base_output_, state, base, frames, matrix_workspace, profile_);
  convolve(refine_input_, combined, state, frames, matrix_workspace, profile_);
  activate(state, 48 * frames, profile_);
  for (const auto& b : refine_blocks_) block(b, state, temporary, expanded, frames, matrix_workspace, profile_);
  convolve(refine_output_, state, output, frames, matrix_workspace, profile_);
  for (std::size_t i = 0; i < 60 * frames; ++i) output[i] += base[i];
  std::copy(base + 60 * frames, base + 64 * frames, output + 60 * frames);
  return finite(output, output_count);
}

bool EnvelopePredictor::to_physical(float* output, std::size_t count, std::size_t frames) const {
  if (!weights_ || !workspace_floats(frames) || count != 64 * frames ||
      !valid_span(output, count) || overlap(output, count, weights_, kWeightFloats) ||
      !finite(output, count)) return false;
  for (std::size_t c = 0; c < 62; ++c)
    for (std::size_t t = 0; t < frames; ++t)
      output[c * frames + t] = output[c * frames + t] * y_std_[c] + y_mean_[c];
  for (std::size_t t = 0; t < frames; ++t) {
    const float probability = 1.0f / (1.0f + std::exp(-output[63 * frames + t]));
    const float log_f0 = std::max(4.174387f, std::min(6.39693f,
                         output[62 * frames + t] * *f0_std_ + *f0_mean_));
    output[62 * frames + t] = probability >= .5f ? std::exp(log_f0) : 0.f;
    output[63 * frames + t] = probability;
  }
  return finite(output, count);
}

}  // namespace sumi_world
