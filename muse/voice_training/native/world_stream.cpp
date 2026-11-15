// Uses the pinned upstream offline pulse synthesis unchanged. Do not separately
// compile synthesis.cpp: its public Synthesis is also available as an audit oracle.
// The original WORLD BSD-3-Clause license accompanies source/binary distributions.
#include "world_stream.h"
#include "world/codec.h"
#include "codec.cpp"
#include "synthesis.cpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <limits>
#include <new>
#ifdef SUMI_WORLD_FFT_DSP
#include "world_fft_dsp.h"
#include <cstdint>
#endif
#ifdef ESP_PLATFORM
#include "esp_timer.h"
#include "esp_heap_caps.h"
#endif

namespace sumi_world {
namespace {
constexpr double kPeriod = 0.005;
constexpr double kPhasePi = 3.1415926535897932384;
constexpr int kMaxFrames = 24000;
unsigned long long tick() {
#ifdef ESP_PLATFORM
  return static_cast<unsigned long long>(esp_timer_get_time());
#else
  return 0;
#endif
}

// interp1's indices and interpolation fractions depend only on fixed axes.
// Keep its exact division and evaluation order, but calculate the geometry
// once rather than allocating three arrays on every decoded frame.
struct FixedInterpolation {
  int index[kWorldCodecBins]{};
  WorldReal fraction[kWorldCodecBins]{};
  int bins = kWorldCodecBins;
  void initialize(const WorldReal* x, int length, const WorldReal* xi, int size = kWorldCodecBins) {
    bins = size;
    histc(x, length, xi, bins, index);
    for (int i = 0; i < bins; ++i) {
      --index[i];
      const int k = index[i];
      fraction[i] = (xi[i] - x[k]) / (x[k + 1] - x[k]);
    }
  }
  void apply(const WorldReal* y, WorldReal* out, int stride = 1) const {
    for (int i = 0; i < bins; i += stride) {
      const int k = index[i];
      out[i / stride] = y[k] + fraction[i] * (y[k + 1] - y[k]);
    }
  }
};

WorldReal codec_exp(WorldReal x) {
#ifdef SUMI_WORLD_LUT_MATH
  return sumi_lut_exp(x);
#elif defined(SUMI_WORLD_BOUNDED_MATH)
  return sumi_fast_exp(x);
#elif defined(SUMI_WORLD_MATH_PROFILE)
  return sumi_profile_exp(x);
#else
  return std::exp(x);
#endif
}
WorldReal codec_pow10(WorldReal x) {
#ifdef SUMI_WORLD_LUT_MATH
  return sumi_lut_pow(10.f, x);
#elif defined(SUMI_WORLD_BOUNDED_MATH)
  return sumi_fast_pow(10.f, x);
#elif defined(SUMI_WORLD_MATH_PROFILE)
  return sumi_profile_pow(10.f, x);
#else
  return std::pow(WorldReal(10), x);
#endif
}

// Reuse WORLD's exact decode formulas and FFT plan across frames. Upstream's
// public single-call API rebuilds all frequency axes and trigonometric weights
// on every call; a stream must initialize them once per utterance instead.
struct CodecState {
  WorldReal mel[kWorldCodecFftSize / 2 + 2]{};
  WorldReal frequency[kWorldCodecBins]{};
  fft_complex weight[kWorldCodecFftSize / 2]{};
  InverseComplexFFT inverse{};
  WorldReal ap_frequency[kWorldCodecBins]{};
  WorldReal ap_coarse_frequency[4]{};
  WorldReal mel_spectrum[kWorldCodecFftSize / 2 + 2]{};
  FixedInterpolation spectral_map, aperiodic_map;
  CodecState() {
    GetParametersForDecoding(world::kFloorFrequency,
        MyMinDouble(kWorldCodecSampleRate / WorldReal(2), world::kCeilFrequency),
        kWorldCodecSampleRate, kWorldCodecFftSize, 60, mel, frequency, weight);
    InitializeInverseComplexFFT(kWorldCodecFftSize / 2, &inverse);
    for (int i = 0; i < kWorldCodecBins; ++i)
      ap_frequency[i] = static_cast<WorldReal>(kWorldCodecSampleRate) / kWorldCodecFftSize * i;
    for (int i = 0; i <= 2; ++i) ap_coarse_frequency[i] = i * world::kFrequencyInterval;
    ap_coarse_frequency[3] = kWorldCodecSampleRate / WorldReal(2);
    if constexpr (kWorldSampleRate != kWorldCodecSampleRate) {
      // The learned mel geometry and AP anchors stay at 22.05 kHz. Only the
      // queried linear-frequency grid changes to the device output rate.
      for (int i = 0; i < kWorldBins; ++i)
        frequency[i] = ap_frequency[i] = static_cast<WorldReal>(kWorldSampleRate) / kWorldFftSize * i;
      spectral_map.initialize(mel, kWorldCodecFftSize / 2 + 2, frequency, kWorldBins);
      aperiodic_map.initialize(ap_coarse_frequency, 4, ap_frequency, kWorldBins);
    } else {
      spectral_map.initialize(mel, kWorldCodecFftSize / 2 + 2, frequency);
      aperiodic_map.initialize(ap_coarse_frequency, 4, ap_frequency);
    }
  }
  ~CodecState() { DestroyInverseComplexFFT(&inverse); }
  bool decode_grid(const float* frame, WorldReal* sp, WorldReal* ap, int stride) {
    if (!frame || !sp || !ap) return false;
    const int bins = (spectral_map.bins - 1) / stride + 1;
    WorldReal coded_sp[60], coded_ap[2];
    for (int i = 0; i < 62; ++i) if (!std::isfinite(frame[i])) return false;
    for (int i = 0; i < 60; ++i) {
      if (std::fabs(frame[i]) > 200.f) return false;
      coded_sp[i] = frame[i];
    }
    for (int i = 0; i < 2; ++i)
      coded_ap[i] = std::clamp(static_cast<WorldReal>(frame[60 + i]), WorldReal(-60), WorldReal(-0.001));
    IDCTForCodec(coded_sp, kWorldCodecFftSize / 2, weight, &inverse, 60, mel_spectrum + 1);
    mel_spectrum[0] = mel_spectrum[1];
    mel_spectrum[kWorldCodecFftSize / 2 + 1] = mel_spectrum[kWorldCodecFftSize / 2];
    spectral_map.apply(mel_spectrum, sp, stride);
    for (int i = 0; i < bins; ++i) sp[i] = codec_exp(sp[i] / (kWorldCodecFftSize / 2));
    WorldReal coarse[4] = {WorldReal(-60), 0, 0, -world::kMySafeGuardMinimum};
    std::fill(ap, ap + bins, WorldReal(1) - world::kMySafeGuardMinimum);
    if (CheckVUV(coded_ap, 2, coarse) != 1) {
      aperiodic_map.apply(coarse, ap, stride);
      for (int i = 0; i < bins; ++i) ap[i] = codec_pow10(ap[i] / WorldReal(20));
    }
    for (int i = 0; i < bins; ++i) {
      if (!std::isfinite(sp[i]) || sp[i] <= 0 || !std::isfinite(ap[i])) return false;
      ap[i] = std::clamp(ap[i], WorldReal(1e-6), WorldReal(1));
    }
    return true;
  }
  bool decode(const float* frame, WorldReal* sp, WorldReal* ap) {
    // Decode the original 1024-point codec, but evaluate nonlinear functions
    // only at bins that synthesis consumes. Each retained value is unchanged.
    return decode_grid(frame, sp, ap, kWorldSampleRate == kWorldCodecSampleRate ? kWorldCodecFftSize / kWorldFftSize : 1);
  }
};

struct Pulse {
  int sample = 0;
  double fraction = 0;
  double voiced = 0;
};

struct FeatureReader {
  const float* physical;
  std::size_t frames, checked = 0;
  FeatureWait wait;
  void* context;
  bool valid = true;
  bool ensure(std::size_t needed) {
    if (!valid || needed > frames) return valid = false;
    if (needed <= checked) return true;
    if (wait && !wait(context, needed)) return valid = false;
    for (std::size_t c = 0; c < 64; ++c) for (std::size_t i = checked; i < needed; ++i) {
      const float v = physical[c * frames + i];
      if (!std::isfinite(v) || (c < 60 && std::fabs(v) > 200) ||
          (c == 62 && (v < 0 || v > 600)) || (c == 63 && (v < 0 || v > 1))) return valid = false;
    }
    checked = needed;
    return true;
  }
};

// Same interpolation, accumulating phase, and fractional pulse location as
// GetTimeBase/GetPulseLocationsForTimeBase, without whole-waveform arrays.
class PulseReader {
 public:
  PulseReader(FeatureReader& features, int frames, int samples)
      : features_(features), f0_(features.physical + 62 * frames), frames_(frames), samples_(samples) {}

  bool next(Pulse* pulse) {
    for (; sample_ < samples_; ++sample_) {
      if (sample_ >= frame_end_sample_) {
        const double time = sample_ / static_cast<double>(kWorldSampleRate);
        while (frame_ < frames_ - 1 && time >= (frame_ + 1) * kPeriod) ++frame_;
        begin_ = frame_ * kPeriod;
        interval_ = (frame_ + 1) * kPeriod - begin_;
        a_ = coarse(frame_); b_ = coarse(frame_ + 1);
        va_ = a_ == 0.0 ? 0.0 : 1.0;
        // Upstream extrapolates voicing independently at the last frame.
        vb_ = frame_ + 1 < frames_ ? (b_ == 0.0 ? 0.0 : 1.0) :
            2.0 * va_ - (coarse(frames_ - 2) == 0.0 ? 0.0 : 1.0);
        if (!features_.valid) return false;
        const double end = (frame_ + 1) * kPeriod;
        frame_end_sample_ = std::min(samples_, static_cast<int>(std::ceil(end * kWorldSampleRate)));
        // Integer bounds remove repeated double comparisons, but retain the
        // exact original time-division predicate at a rounded frame boundary.
        while (frame_end_sample_ > sample_ &&
               (frame_end_sample_ - 1) / static_cast<double>(kWorldSampleRate) >= end) --frame_end_sample_;
        while (frame_end_sample_ < samples_ &&
               frame_end_sample_ / static_cast<double>(kWorldSampleRate) < end) ++frame_end_sample_;
#ifdef SUMI_WORLD_PHASE_RECURRENCE
        // Re-anchor every 5 ms so roundoff cannot drift across the utterance.
        // The linear F0 ramp has a constant phase-increment difference.
        if (va_ == 1.0 && vb_ == 1.0) {
          const double s = (time - begin_) / interval_;
          increment_ = 2.0 * kPhasePi * (a_ + s * (b_ - a_)) / kWorldSampleRate;
          increment_step_ = 2.0 * kPhasePi * ((b_ - a_) / interval_ / kWorldSampleRate) / kWorldSampleRate;
        }
#endif
      }
      double voiced = va_, f0 = world::kDefaultF0;
#ifdef SUMI_WORLD_PHASE_RECURRENCE
      if (va_ == 1.0 && vb_ == 1.0) {
        phase_ += increment_;
        increment_ += increment_step_;
      } else {
#endif
      if (va_ != 0 || vb_ != 0) {
        const double time = sample_ / static_cast<double>(kWorldSampleRate);
        const double s = (time - begin_) / interval_;
        voiced = va_ == vb_ ? va_ : (va_ + s * (vb_ - va_) > 0.5 ? 1.0 : 0.0);
        if (voiced != 0) f0 = a_ + s * (b_ - a_);
      }
      phase_ += voiced == 0 ? 2.0 * kPhasePi * world::kDefaultF0 / kWorldSampleRate :
          2.0 * kPhasePi * f0 / kWorldSampleRate;
#ifdef SUMI_WORLD_PHASE_RECURRENCE
      }
#endif
      // Most samples cannot cross the next cycle. Keep the original double
      // accumulation exactly, and run the original fmod test only near a
      // crossing. A generous ULP guard covers rounded boundary multiplication.
      bool found = false;
      double wrapped = 0, previous_wrapped = 0;
      if (phase_ >= next_threshold_) {
        wrapped = std::fmod(phase_, 2.0 * kPhasePi);
        previous_wrapped = std::fmod(previous_total_, 2.0 * kPhasePi);
        found = sample_ > 0 && std::fabs(wrapped - previous_wrapped) > kPhasePi;
      }
      if (found) {
        pulse->sample = sample_ - 1;
        const double y1 = previous_wrapped - 2.0 * kPhasePi;
        pulse->fraction = -y1 / (wrapped - y1) / kWorldSampleRate;
        pulse->voiced = previous_voiced_;
        next_boundary_ = ++cycle_ * (2.0 * kPhasePi);
        next_threshold_ = next_boundary_ - 64 * std::numeric_limits<double>::epsilon() * next_boundary_;
      }
      previous_total_ = phase_;
      previous_voiced_ = voiced;
      if (found) { ++sample_; return true; }
    }
    return false;
  }

 private:
  double coarse(int frame) const {
    if (frame == frames_) return 2.0 * coarse(frames_ - 1) - coarse(frames_ - 2);
    if (!features_.ensure(frame + 1)) return 0;
    return f0_[frame] < kWorldSampleRate / kWorldFftSize + 1.0 ? 0.0 : f0_[frame];
  }
  FeatureReader& features_;
  const float* f0_;
  int frames_, samples_, sample_ = 0, frame_ = 0;
  int frame_end_sample_ = 0;
  double begin_ = 0, interval_ = kPeriod, a_ = 0, b_ = 0, va_ = 0, vb_ = 0;
  double phase_ = 0, previous_total_ = 0, previous_voiced_ = 0;
#ifdef SUMI_WORLD_PHASE_RECURRENCE
  double increment_ = 0, increment_step_ = 0;
#endif
  unsigned cycle_ = 1;
  double next_boundary_ = 2.0 * kPhasePi;
  double next_threshold_ = next_boundary_ - 64 * std::numeric_limits<double>::epsilon() * next_boundary_;
};

struct State {
  CodecState codec;
  MinimumPhaseAnalysis minimum_phase{};
  InverseRealFFT inverse{};
  ForwardRealFFT forward{};
  RandnState random{};
  WorldReal dc[kWorldFftSize]{};
  WorldReal periodic[kWorldFftSize]{};
  WorldReal aperiodic[kWorldFftSize]{};
  WorldReal spectrum[kWorldBins]{};
  WorldReal ratio[kWorldBins]{};
  WorldReal decoded_sp[2][kWorldBins]{};
  WorldReal decoded_ap[2][kWorldBins]{};
  int decoded_index[2] = {-1, -1};
#if SUMI_WORLD_FILTER_STRIDE
  // Experimental: interpolate minimum-phase filters on the feature grid,
  // instead of recomputing both filters at every glottal/noise pulse. This
  // changes the waveform and requires a separate listening evaluation.
  using FrameFilters = fft_complex[kWorldBins];
  FrameFilters* filters[2]{};
  int filter_index[2] = {-1, -1}, filter_voiced[2] = {-1, -1};
#endif
  WorldReal overlap[kWorldFftSize]{};
  float block[kWorldFftSize]{};
  std::size_t emitted = 0, block_used = 0;
  const float* physical;
  FeatureReader& features;
  std::size_t frames, samples, block_size;
  WaveSink sink;
  void* context;
  SynthesisStats stats{};
#ifdef SUMI_WORLD_FFT_DSP
  // DSP plans copy their input to private FFT scratch before writing outputs.
  // The real log spectrum, cepstrum, phase spectrum and inverse waveform can
  // therefore share storage. Noise needs a separate half spectrum until mixed.
  static constexpr std::size_t kPhaseFloats = 2 * kWorldBins + 2;
  WorldReal* hot = nullptr;
  WorldReal* noise_hot = nullptr;
  WorldReal* allocate_hot(std::size_t floats = kPhaseFloats) {
    const std::size_t bytes = (floats * sizeof(WorldReal) + 15) / 16 * 16;
#ifdef ESP_PLATFORM
    WorldReal* result = nullptr;
    #ifndef SUMI_WORLD_INTERNAL_RESERVE
#define SUMI_WORLD_INTERNAL_RESERVE 12000
#endif
    if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) > bytes + SUMI_WORLD_INTERNAL_RESERVE)
      result = static_cast<float*>(heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (result) stats.internal_state_bytes += bytes;
    else result = static_cast<float*>(heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    return result;
#else
    return static_cast<float*>(std::aligned_alloc(16, bytes));
#endif
  }
#endif

  State(FeatureReader& source, std::size_t n, std::size_t length, std::size_t output_block,
        WaveSink callback, void* ctx)
      : physical(source.physical), features(source), frames(n), samples(length), block_size(output_block), sink(callback), context(ctx) {
#ifdef SUMI_WORLD_FFT_DSP
    hot = allocate_hot(); noise_hot = allocate_hot();
    if (!hot || !noise_hot) return;
#if SUMI_WORLD_FILTER_STRIDE
    for (auto& filter : filters)
      filter = reinterpret_cast<FrameFilters*>(allocate_hot(2 * kWorldBins * 2));
    if (!filters[0] || !filters[1]) return;
#endif
    minimum_phase.fft_size = inverse.fft_size = forward.fft_size = kWorldFftSize;
    minimum_phase.log_spectrum = hot;
    minimum_phase.cepstrum = minimum_phase.minimum_phase_spectrum = reinterpret_cast<fft_complex*>(hot);
    minimum_phase.inverse_fft = fft_plan_dft_r2c_1d(kWorldFftSize, hot, minimum_phase.cepstrum, FFT_ESTIMATE);
    minimum_phase.forward_fft = fft_plan_dft_1d(kWorldFftSize, minimum_phase.cepstrum, minimum_phase.minimum_phase_spectrum, FFT_FORWARD, FFT_ESTIMATE);
    inverse.waveform = hot;
    inverse.spectrum = reinterpret_cast<fft_complex*>(hot);
    inverse.inverse_fft = fft_plan_dft_c2r_1d(kWorldFftSize, inverse.spectrum, inverse.waveform, FFT_ESTIMATE);
    forward.waveform = noise_hot;
    forward.spectrum = reinterpret_cast<fft_complex*>(forward.waveform);
    forward.forward_fft = fft_plan_dft_r2c_1d(kWorldFftSize, forward.waveform, forward.spectrum, FFT_ESTIMATE);
#else
    InitializeMinimumPhaseAnalysis(kWorldFftSize, &minimum_phase);
    InitializeInverseRealFFT(kWorldFftSize, &inverse);
    InitializeForwardRealFFT(kWorldFftSize, &forward);
#endif
    randn_reseed(&random);
    GetDCRemover(kWorldFftSize, dc);
    stats.wrapper_state_bytes = sizeof(*this);
  }
  ~State() {
#ifdef SUMI_WORLD_FFT_DSP
#ifdef ESP_PLATFORM
    heap_caps_free(hot);
    heap_caps_free(noise_hot);
#if SUMI_WORLD_FILTER_STRIDE
    for (auto* filter : filters) heap_caps_free(filter);
#endif
#else
    std::free(hot);
    std::free(noise_hot);
#if SUMI_WORLD_FILTER_STRIDE
    for (auto* filter : filters) std::free(filter);
#endif
#endif
#else
    DestroyMinimumPhaseAnalysis(&minimum_phase);
    DestroyInverseRealFFT(&inverse);
    DestroyForwardRealFFT(&forward);
#endif
  }
  bool decode(int index, int slot) {
    if (decoded_index[slot] == index) return true;
    if (!features.ensure(index + 1)) return false;
    float frame[64];
    for (int c = 0; c < 64; ++c) frame[c] = physical[c * frames + index];
    if (!codec.decode(frame, decoded_sp[slot], decoded_ap[slot])) return false;
    decoded_index[slot] = index;
    ++stats.decoded_frames;
    return true;
  }
#if SUMI_WORLD_FILTER_STRIDE
  void prepare_filter(int index, int slot, bool voiced) {
    if (filter_index[slot] == index && filter_voiced[slot] == int(voiced)) return;
    for (int kind = voiced ? 0 : 1; kind < 2; ++kind) {
      for (int i = 0; i < kWorldBins; ++i) {
        const WorldReal sp = std::fabs(decoded_sp[slot][i]);
        const WorldReal ap = GetSafeAperiodicity(decoded_ap[slot][i]);
        const WorldReal ratio = ap * ap;
        const WorldReal value = kind == 0 ? sp * (1.f - ratio) + world::kMySafeGuardMinimum :
            voiced ? sp * ratio : sp;
        minimum_phase.log_spectrum[i] = sumi_lut_log(value) * .5f;
      }
      GetMinimumPhaseSpectrum(&minimum_phase);
      for (int i = 0; i < kWorldBins; ++i) for (int part = 0; part < 2; ++part)
        filters[slot][kind][i][part] = minimum_phase.minimum_phase_spectrum[i][part];
    }
    filter_index[slot] = index; filter_voiced[slot] = int(voiced);
    ++stats.filter_frames;
  }
  void filtered_response(int left, int right, int lslot, int rslot, WorldReal fraction,
                         const Pulse& pulse, int noise_size) {
    const bool voiced = pulse.voiced > .5;
    prepare_filter(left, lslot, voiced);
    prepare_filter(right, rslot, voiced);
    const WorldReal blend = (1.f - fraction) * GetSafeAperiodicity(decoded_ap[lslot][0]) +
        fraction * GetSafeAperiodicity(decoded_ap[rslot][0]);
    if (!voiced || blend * blend > .999f) {
      std::fill(periodic, periodic + kWorldFftSize, 0.f);
    } else {
      for (int i = 0; i < kWorldBins; ++i) for (int part = 0; part < 2; ++part)
        inverse.spectrum[i][part] = (1.f - fraction) * filters[lslot][0][i][part] +
            fraction * filters[rslot][0][i][part];
      const WorldReal coefficient = 2.f * world::kPi * static_cast<WorldReal>(pulse.fraction) *
          kWorldSampleRate / kWorldFftSize;
      for (int i = 0; i < kWorldBins; ++i) {
        float sine, cosine;
        sumi_lut_sincos(coefficient * i, &sine, &cosine);
        sine = std::fabs(sine);
        const float re = inverse.spectrum[i][0], im = inverse.spectrum[i][1];
        inverse.spectrum[i][0] = re * cosine + im * sine;
        inverse.spectrum[i][1] = im * cosine - re * sine;
      }
      fft_execute(inverse.inverse_fft);
      // The DC remover discards the shifted first half. Fuse that copy while
      // retaining the original summation and subtraction order exactly.
      float dc_component = 0;
      for (int i = 0; i < kWorldFftSize / 2; ++i) dc_component += inverse.waveform[i];
      for (int i = 0; i < kWorldFftSize / 2; ++i) periodic[i] = -dc_component * dc[i];
      for (int i = kWorldFftSize / 2; i < kWorldFftSize; ++i)
        periodic[i] = inverse.waveform[i - kWorldFftSize / 2] - dc_component * dc[i];
    }
    GetNoiseSpectrum(noise_size, kWorldFftSize, &forward, &random);
    for (int i = 0; i < kWorldBins; ++i) {
      const WorldReal re = (1.f - fraction) * filters[lslot][1][i][0] + fraction * filters[rslot][1][i][0];
      const WorldReal im = (1.f - fraction) * filters[lslot][1][i][1] + fraction * filters[rslot][1][i][1];
      inverse.spectrum[i][0] = re * forward.spectrum[i][0] - im * forward.spectrum[i][1];
      inverse.spectrum[i][1] = re * forward.spectrum[i][1] + im * forward.spectrum[i][0];
    }
    fft_execute(inverse.inverse_fft);
    // The overlap/add loop can consume this internal-RAM result directly.
  }
#endif
  bool flush(std::size_t stop) {
    stop = std::min(stop, samples);
    while (emitted < stop) {
      const std::size_t index = emitted % kWorldFftSize;
      const float value = static_cast<float>(overlap[index]);
      if (!std::isfinite(value)) return false;
      block[block_used++] = value;
      overlap[index] = 0;
      ++emitted;
      if (block_used == block_size) {
        if (!sink(context, block, block_used)) return false;
        stats.output_samples += block_used;
        block_used = 0;
      }
    }
    return true;
  }
  bool pulse(const Pulse& p, int noise_size) {
    if (noise_size < 0 || noise_size > kWorldFftSize) return false;
    const int offset = p.sample - kWorldFftSize / 2 + 1;
    if (!flush(static_cast<std::size_t>(std::max(0, offset)))) return false;
    const double time = p.sample / static_cast<double>(kWorldSampleRate);
#if SUMI_WORLD_FILTER_STRIDE
    const int left = std::min(static_cast<int>(frames) - 1,
        static_cast<int>(std::floor(time / kPeriod)) / SUMI_WORLD_FILTER_STRIDE * SUMI_WORLD_FILTER_STRIDE);
    const int right = std::min(static_cast<int>(frames) - 1, left + SUMI_WORLD_FILTER_STRIDE);
    const int lslot = (left / SUMI_WORLD_FILTER_STRIDE) % 2, rslot = left == right ? lslot : 1 - lslot;
#else
    const int left = std::min(static_cast<int>(frames) - 1, static_cast<int>(std::floor(time / kPeriod)));
    const int right = std::min(static_cast<int>(frames) - 1, static_cast<int>(std::ceil(time / kPeriod)));
    const int lslot = left % 2, rslot = right % 2;
#endif
    const auto decode_started = tick();
    if (!decode(left, lslot) || !decode(right, rslot)) return false;
    const auto response_started = tick();
    stats.decode_us += response_started - decode_started;
#if SUMI_WORLD_FILTER_STRIDE
    const WorldReal fraction = left == right ? 0.f : static_cast<WorldReal>((time / kPeriod - left) / (right - left));
    filtered_response(left, right, lslot, rslot, fraction, p, noise_size);
#else
    const WorldReal fraction = static_cast<WorldReal>(time / kPeriod - left);
    for (int i = 0; i < kWorldBins; ++i) {
      if (left == right) {
        spectrum[i] = std::fabs(decoded_sp[left % 2][i]);
        ratio[i] = std::pow(GetSafeAperiodicity(decoded_ap[left % 2][i]), WorldReal(2));
      } else {
        spectrum[i] = (WorldReal(1) - fraction) * std::fabs(decoded_sp[left % 2][i]) +
            fraction * std::fabs(decoded_sp[right % 2][i]);
        ratio[i] = std::pow((WorldReal(1) - fraction) * GetSafeAperiodicity(decoded_ap[left % 2][i]) +
            fraction * GetSafeAperiodicity(decoded_ap[right % 2][i]), WorldReal(2));
      }
    }
    GetPeriodicResponse(kWorldFftSize, spectrum, ratio, p.voiced, &inverse, &minimum_phase,
                        dc, p.fraction, kWorldSampleRate, periodic);
    GetAperiodicResponse(noise_size, kWorldFftSize, spectrum, ratio, p.voiced, &forward,
                        &inverse, &minimum_phase, aperiodic, &random);
#endif
#ifdef SUMI_WORLD_FFT_DSP
    if (!fft_dsp_ok()) return false;
#endif
    const WorldReal gain = std::sqrt(static_cast<WorldReal>(noise_size));
    const int lower = std::max(0, -offset);
    const int upper = std::min(kWorldFftSize, static_cast<int>(samples) - offset);
    for (int j = lower; j < upper; ++j) {
#if SUMI_WORLD_FILTER_STRIDE
      const WorldReal noise = inverse.waveform[(j + kWorldFftSize / 2) % kWorldFftSize];
#else
      const WorldReal noise = aperiodic[j];
#endif
      overlap[(j + offset) % kWorldFftSize] += (periodic[j] * gain + noise) / kWorldFftSize;
    }
    ++stats.pulses;
    stats.response_us += tick() - response_started;
    return true;
  }
};
}  // namespace

bool decode_world_frame(const float* frame, WorldReal* sp, WorldReal* ap) {
  if (!frame || !sp || !ap) return false;
#ifdef SUMI_WORLD_FFT_DSP
  FftDspSession fft_session;
  if (!fft_session.begin(nullptr, 0)) return false;
#endif
  if constexpr (kWorldFftSize != kWorldCodecFftSize || kWorldSampleRate != kWorldCodecSampleRate) {
    CodecState codec;
    return codec.decode(frame, sp, ap);
  }
  WorldReal coded_sp[60], coded_ap[2];
  for (int i = 0; i < 62; ++i) if (!std::isfinite(frame[i])) return false;
  for (int i = 0; i < 60; ++i) {
    if (std::fabs(frame[i]) > 200.0f) return false;
    coded_sp[i] = frame[i];
  }
  for (int i = 0; i < 2; ++i) coded_ap[i] = std::clamp(static_cast<WorldReal>(frame[60 + i]), WorldReal(-60), WorldReal(-0.001));
  const WorldReal* coded_sp_rows[] = {coded_sp};
  const WorldReal* coded_ap_rows[] = {coded_ap};
  WorldReal* sp_rows[] = {sp};
  WorldReal* ap_rows[] = {ap};
  DecodeSpectralEnvelope(coded_sp_rows, 1, kWorldSampleRate, kWorldFftSize, 60, sp_rows);
  DecodeAperiodicity(coded_ap_rows, 1, kWorldSampleRate, kWorldFftSize, ap_rows);
  for (int i = 0; i < kWorldBins; ++i) {
    if (!std::isfinite(sp[i]) || sp[i] <= 0 || !std::isfinite(ap[i])) return false;
    ap[i] = std::clamp(ap[i], WorldReal(1e-6), WorldReal(1));
  }
  return true;
}

bool synthesize_stream(const float* physical, std::size_t count, std::size_t frames,
                       std::size_t samples, std::size_t block, WaveSink sink, void* ctx,
                       SynthesisStats* stats, float* fft_workspace, std::size_t fft_workspace_count,
                       FeatureWait wait_features, void* feature_context) {
  if (stats) *stats = {};
  if (!physical || !sink || frames < 2 || frames > kMaxFrames || count != frames * 64 ||
      samples == 0 || samples > (frames - 1) * kWorldSampleRate / 200 ||
      block == 0 || block > kWorldFftSize) return false;
  FeatureReader features{physical, frames, 0, wait_features, feature_context};
  if (!features.ensure(wait_features ? 2 : frames)) return false;
#ifdef SUMI_WORLD_FFT_DSP
  FftDspSession fft_session;
  if (fft_workspace) {
    const auto a = reinterpret_cast<std::uintptr_t>(physical);
    const auto b = reinterpret_cast<std::uintptr_t>(fft_workspace);
    if (a < b + kFftWorkspaceFloats * 4 && b < a + count * 4) return false;
  }
  if (!fft_session.begin(fft_workspace, fft_workspace_count)) return false;
#else
  (void)fft_workspace; (void)fft_workspace_count;
#endif
  std::unique_ptr<State> state(new (std::nothrow) State(features, frames, samples, block, sink, ctx));
  if (!state) return false;
#ifdef SUMI_WORLD_FFT_DSP
  if (!state->hot || !state->noise_hot) return false;
#if SUMI_WORLD_FILTER_STRIDE
  if (!state->filters[0] || !state->filters[1]) return false;
#endif
#endif
  const int full_samples = static_cast<int>(frames * 5.0 * kWorldSampleRate / 1000);
  PulseReader reader(features, static_cast<int>(frames), full_samples);
  Pulse current, next;
  auto phase_started = tick();
  bool present = reader.next(&current);
  if (!features.valid) return false;
  state->stats.phase_us += tick() - phase_started;
  while (present) {
    // Later pulses cannot affect the requested prefix. Do not spend device
    // time synthesizing the omitted tail (including short diagnostic runs).
    if (current.sample - kWorldFftSize / 2 + 1 >= static_cast<int>(samples)) break;
    phase_started = tick();
    const bool more = reader.next(&next);
    if (!features.valid) return false;
    state->stats.phase_us += tick() - phase_started;
    if (!state->pulse(current, more ? next.sample - current.sample : 0)) return false;
    present = more;
    current = next;
  }
  if (!state->flush(samples)) return false;
  if (state->block_used) {
    if (!sink(ctx, state->block, state->block_used)) return false;
    state->stats.output_samples += state->block_used;
  }
  if (stats) *stats = state->stats;
#ifdef SUMI_WORLD_FFT_DSP
  if (!fft_session.ok()) return false;
#endif
  return true;
}
}  // namespace sumi_world
