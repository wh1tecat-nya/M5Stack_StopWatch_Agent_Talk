// Adapter preserving WORLD/Ooura's unusual complex conjugation conventions.
// Host fallback validates the mapping; C152 uses Espressif's optimized kernel.
#include "world_fft_dsp.h"
#include "world/fft.h"
#include "world_math_tables.h"
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <utility>
#ifdef ESP_PLATFORM
#include "dsps_fft2r.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#endif

namespace {
constexpr int kComplexSize = sumi_world::kFftMaxComplexSize;
constexpr int kTwiddleOffset = 2 * kComplexSize;
std::atomic_flag busy = ATOMIC_FLAG_INIT;
float* work = nullptr;
bool failed = false;
sumi_world::FftProfile profile;
unsigned long long fft_time() {
#ifdef ESP_PLATFORM
  return esp_timer_get_time();
#else
  return 0;
#endif
}

bool transform(int n) {
#ifdef ESP_PLATFORM
  return dsps_fft2r_fc32(work, n) == ESP_OK &&
      (n >= 16 ? dsps_bit_rev2r_fc32(work, n) : dsps_bit_rev_fc32(work, n)) == ESP_OK;
#else
  // Conventional, unnormalized complex FFT with negative exponent. The
  // device's ESP-DSP implementation is verified separately on the fixture.
  for (int i = 1, j = 0; i < n; ++i) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      std::swap(work[2 * i], work[2 * j]);
      std::swap(work[2 * i + 1], work[2 * j + 1]);
    }
  }
  for (int len = 2; len <= n; len <<= 1) for (int start = 0; start < n; start += len) {
    for (int j = 0; j < len / 2; ++j) {
      const int index = j * kComplexSize / len;
      const float re = work[kTwiddleOffset + index * 2], im = -work[kTwiddleOffset + index * 2 + 1];
      const int a = 2 * (start + j), b = 2 * (start + j + len / 2);
      const float vr = work[b] * re - work[b + 1] * im;
      const float vi = work[b] * im + work[b + 1] * re;
      work[b] = work[a] - vr; work[b + 1] = work[a + 1] - vi;
      work[a] += vr; work[a + 1] += vi;
    }
  }
  return true;
#endif
}

// Real FFT: pack even/odd samples into one complex FFT of half the size.
// Only positive-frequency bins are needed by WORLD. Inverse scaling remains
// unnormalized by N, matching the original API and its overlap/add gain.
bool real_transform(fft_plan p) {
  const int n = p.n, half = n / 2, step = 2048 / n;
  if (!p.c_in) {
    for (int i = 0; i < n; ++i) work[i] = p.in[i];
  } else {
    work[0] = p.c_in[0][0] + p.c_in[half][0];
    work[1] = p.c_in[half][0] - p.c_in[0][0];
    for (int k = 1; k <= half / 2; ++k) {
      const float c = sumi_world_lut::kSine[(k * step + 512) & 2047];
      const float s = sumi_world_lut::kSine[k * step];
      const float ar = p.c_in[k][0], ai = p.c_in[k][1];
      const float br = p.c_in[half - k][0], bi = -p.c_in[half - k][1];
      const float dr = ar - br, di = ai - bi;
      const float real_sum = ar + br, imag_sum = ai + bi;
      const float p = c * di + s * dr, q = c * dr - s * di;
      work[2 * k] = real_sum - p;
      work[2 * k + 1] = -imag_sum - q;
      if (k != half - k) {
        work[2 * (half - k)] = real_sum + p;
        work[2 * (half - k) + 1] = imag_sum - q;
      }
    }
  }
  const auto started = fft_time();
  if (!transform(half)) return false;
  profile.transform_us += fft_time() - started;
  if (p.c_in) {
    for (int i = 0; i < half; ++i) {
      p.out[2 * i] = work[2 * i];
      p.out[2 * i + 1] = -work[2 * i + 1];
    }
  } else {
    p.c_out[0][0] = work[0] + work[1];
    p.c_out[half][0] = work[0] - work[1];
    p.c_out[0][1] = p.c_out[half][1] = 0;
    for (int k = 1; k <= half / 2; ++k) {
      const float c = sumi_world_lut::kSine[(k * step + 512) & 2047];
      const float s = sumi_world_lut::kSine[k * step];
      const float ar = work[2 * k], ai = work[2 * k + 1];
      const float br = work[2 * (half - k)], bi = -work[2 * (half - k) + 1];
      const float er = .5f * (ar + br), ei = .5f * (ai + bi);
      const float or_ = .5f * (ai - bi), oi = -.5f * (ar - br);
      const float real_part = c * or_ + s * oi, imag_part = c * oi - s * or_;
      p.c_out[k][0] = er + real_part;
      p.c_out[k][1] = ei + imag_part;
      if (k != half - k) {
        p.c_out[half - k][0] = er - real_part;
        p.c_out[half - k][1] = -ei + imag_part;
      }
    }
  }
  return true;
}
}

namespace sumi_world {
bool FftDspSession::begin(float* workspace, std::size_t count) {
  if (acquired_ || busy.test_and_set()) return false;
  acquired_ = true;
  failed = false;
  profile = {};
  if (workspace && (count < kFftWorkspaceFloats || reinterpret_cast<std::uintptr_t>(workspace) % 16)) return false;
  if (!workspace) {
    if (count) return false;
#ifdef ESP_PLATFORM
    owned_ = static_cast<float*>(heap_caps_aligned_alloc(16, kFftWorkspaceFloats * 4,
        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
#else
    owned_ = static_cast<float*>(std::aligned_alloc(16, kFftWorkspaceFloats * 4));
#endif
    workspace = owned_;
  }
  if (!workspace) return false;
  work = workspace;
#ifdef ESP_PLATFORM
  if (dsps_fft2r_initialized) {
    work = nullptr;
    return false;
  }
  if (dsps_fft2r_init_fc32(work + kTwiddleOffset, kComplexSize) != ESP_OK) {
    dsps_fft2r_deinit_fc32(); work = nullptr; return false;
  }
#else
  for (int i = 0; i < kComplexSize / 2; ++i) {
    const double angle = 2 * 3.14159265358979323846 * i / kComplexSize;
    work[kTwiddleOffset + i * 2] = static_cast<float>(std::cos(angle));
    work[kTwiddleOffset + i * 2 + 1] = static_cast<float>(std::sin(angle));
  }
#endif
  return true;
}
bool FftDspSession::ok() const { return acquired_ && work && !failed; }
FftProfile fft_dsp_profile() { return profile; }
bool fft_dsp_ok() { return work && !failed; }
FftDspSession::~FftDspSession() {
  if (!acquired_) return;
#ifdef ESP_PLATFORM
  if (work) dsps_fft2r_deinit_fc32();
  heap_caps_free(owned_);
#else
  std::free(owned_);
#endif
  work = nullptr;
  busy.clear();
}
}

fft_plan fft_plan_dft_1d(int n, fft_complex* in, fft_complex* out, int sign, unsigned int flags) {
  fft_plan p{}; p.n = n; p.c_in = in; p.c_out = out; p.sign = sign; p.flags = flags; return p;
}
fft_plan fft_plan_dft_r2c_1d(int n, float* in, fft_complex* out, unsigned int flags) {
  fft_plan p{}; p.n = n; p.in = in; p.c_out = out; p.sign = FFT_FORWARD; p.flags = flags; return p;
}
fft_plan fft_plan_dft_c2r_1d(int n, fft_complex* in, float* out, unsigned int flags) {
  fft_plan p{}; p.n = n; p.c_in = in; p.out = out; p.sign = FFT_BACKWARD; p.flags = flags; return p;
}
void fft_destroy_plan(fft_plan) {}

void fft_execute(fft_plan p) {
  if (!work || p.n < 2 || p.n > 1024 || (p.n & (p.n - 1))) { failed = true; return; }
  const int n = p.n;
  const auto started = fft_time();
  if (n >= 4 && (!p.c_in || !p.c_out)) {
    if (!real_transform(p)) { failed = true; return; }
    ++profile.calls; profile.total_us += fft_time() - started;
    return;
  }
  if (n > kComplexSize) { failed = true; return; }
  if (!p.c_in) {
    for (int i = 0; i < n; ++i) { work[2 * i] = p.in[i]; work[2 * i + 1] = 0; }
  } else if (!p.c_out) {
    // Conjugate the Hermitian input; real(FFT(conj(X))) is unscaled IFFT(X).
    for (int i = 0; i <= n / 2; ++i) {
      work[2 * i] = p.c_in[i][0];
      work[2 * i + 1] = (i == 0 || i == n / 2) ? 0 : -p.c_in[i][1];
    }
    for (int i = n / 2 + 1; i < n; ++i) {
      work[2 * i] = p.c_in[n - i][0]; work[2 * i + 1] = p.c_in[n - i][1];
    }
  } else {
    // WORLD forward = FFT(conj(input)); backward = conj(FFT(input)).
    for (int i = 0; i < n; ++i) {
      work[2 * i] = p.c_in[i][0];
      work[2 * i + 1] = p.sign == FFT_FORWARD ? -p.c_in[i][1] : p.c_in[i][1];
    }
  }
  const auto transform_started = fft_time();
  if (!transform(n)) { failed = true; return; }
  profile.transform_us += fft_time() - transform_started;
  if (!p.c_out) {
    for (int i = 0; i < n; ++i) p.out[i] = work[2 * i];
  } else {
    const int count = p.c_in ? n : n / 2 + 1;
    for (int i = 0; i < count; ++i) {
      p.c_out[i][0] = work[2 * i];
      p.c_out[i][1] = p.sign == FFT_BACKWARD ? -work[2 * i + 1] : work[2 * i + 1];
    }
    if (!p.c_in) p.c_out[0][1] = p.c_out[n / 2][1] = 0;
  }
  ++profile.calls; profile.total_us += fft_time() - started;
}
