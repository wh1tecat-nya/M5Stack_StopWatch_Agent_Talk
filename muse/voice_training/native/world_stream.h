#pragma once

#include <cstddef>

namespace sumi_world {
constexpr int kWorldCodecSampleRate = 22050;
#ifndef SUMI_WORLD_SAMPLE_RATE
#define SUMI_WORLD_SAMPLE_RATE 22050
#endif
constexpr int kWorldSampleRate = SUMI_WORLD_SAMPLE_RATE;
static_assert(kWorldSampleRate == 16000 || kWorldSampleRate == 22050);
#ifndef SUMI_WORLD_SYNTH_FFT_SIZE
#define SUMI_WORLD_SYNTH_FFT_SIZE 1024
#endif
// The trained codec always has 1024-point geometry. Smaller synthesis sizes
// are listening experiments only; they never reinterpret the learned codes.
constexpr int kWorldCodecFftSize = 1024;
constexpr int kWorldCodecBins = 513;
constexpr int kWorldFftSize = SUMI_WORLD_SYNTH_FFT_SIZE;
static_assert(kWorldFftSize == 512 || kWorldFftSize == 1024 ||
              (kWorldFftSize == 256 && kWorldSampleRate == 16000));
constexpr int kWorldBins = kWorldFftSize / 2 + 1;
#ifndef SUMI_WORLD_FILTER_STRIDE
#define SUMI_WORLD_FILTER_STRIDE 0
#endif
static_assert(SUMI_WORLD_FILTER_STRIDE >= 0 && SUMI_WORLD_FILTER_STRIDE <= 2);
#if SUMI_WORLD_FILTER_STRIDE && (!defined(SUMI_WORLD_FP32) || !defined(SUMI_WORLD_FFT_DSP) || !defined(SUMI_WORLD_LUT_MATH))
#error "Experimental frame filters require FP32, DSP FFT and LUT math"
#endif
#ifdef SUMI_WORLD_FP32
using WorldReal = float;
#else
using WorldReal = double;
#endif

// Input is immutable, channel-major physical output from EnvelopePredictor.
// Callback receives temporary float samples (not normalized/clipped), and may
// return false to cancel. No input/output buffers may overlap the model input.
using WaveSink = bool (*)(void* context, const float* samples, std::size_t count);
// The producer publishes a contiguous prefix with release/acquire ordering.
// Published frames must remain immutable until synthesis returns. Returning
// false cancels synthesis; unpublished frames are never read.
using FeatureWait = bool (*)(void* context, std::size_t frames_needed);
struct SynthesisStats {
  std::size_t wrapper_state_bytes = 0;  // Excludes WORLD's internal FFT/codec allocations.
  std::size_t internal_state_bytes = 0;
  std::size_t decoded_frames = 0;
  std::size_t pulses = 0;
  std::size_t filter_frames = 0;
  std::size_t output_samples = 0;
  unsigned long long decode_us = 0, response_us = 0, phase_us = 0;
};

bool decode_world_frame(const float* frame64, WorldReal* spectrum513, WorldReal* aperiodicity513);

// Waveform memory is bounded independently of duration; the caller currently
// retains the full compact 64-channel physical features. 2..24000 frames (120s).
// WORLD FFT initialization and codec calls allocate internally; not an OOM-safe
// hard real-time API. The device runner must check available memory beforehand.
bool synthesize_stream(const float* physical, std::size_t count, std::size_t frames,
                       std::size_t samples, std::size_t output_block,
                       WaveSink sink, void* context, SynthesisStats* stats,
                       float* fft_workspace = nullptr, std::size_t fft_workspace_count = 0,
                       FeatureWait wait_features = nullptr, void* feature_context = nullptr);
}  // namespace sumi_world
