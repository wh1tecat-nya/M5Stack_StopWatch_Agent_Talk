#include "world_fft_dsp.h"
#include "world/fft.h"
#include "world_math_tables.h"
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <vector>

void require(bool ok) { if (!ok) std::abort(); }
int main() {
  // Paired real-FFT reconstruction relies on these frozen twiddle symmetries.
  // Exclude the self-paired quarter wave where the tiny rounded cosine remains.
  for (int k = 1; k < 512; ++k) {
    require(sumi_world_lut::kSine[k] == sumi_world_lut::kSine[1024 - k]);
    require(sumi_world_lut::kSine[512 + k] == -sumi_world_lut::kSine[1536 - k]);
  }
  for (int n : {2, 4, 8, 16, 32, 512, 1024}) {
    sumi_world::FftDspSession session;
    require(session.begin(nullptr, 0));
    { sumi_world::FftDspSession nested; require(!nested.begin(nullptr, 0)); }
    require(session.ok());
    std::vector<float> raw(2 * n), result(2 * n), real(n), roundtrip(n);
    auto* in = reinterpret_cast<fft_complex*>(raw.data());
    auto* out = reinterpret_cast<fft_complex*>(result.data());
    for (int i = 0; i < n; ++i) {
      in[i][0] = std::sin(.07f * i) + .1f * std::cos(.91f * i);
      in[i][1] = .3f * std::cos(.03f * i);
      real[i] = in[i][0];
    }
    for (int sign : {FFT_FORWARD, FFT_BACKWARD}) {
      if (n > sumi_world::kFftMaxComplexSize) continue;
      auto plan = fft_plan_dft_1d(n, in, out, sign, FFT_ESTIMATE);
      fft_execute(plan); fft_destroy_plan(plan);
      double error = 0, energy = 0;
      for (int k = 0; k < n; ++k) {
        std::complex<double> expected{};
        for (int i = 0; i < n; ++i) {
          const auto x = std::complex<double>(in[i][0], sign == FFT_FORWARD ? -in[i][1] : in[i][1]);
          expected += x * std::polar(1.0, -2 * 3.14159265358979323846 * i * k / n);
        }
        if (sign == FFT_BACKWARD) expected = std::conj(expected);
        error += std::norm(std::complex<double>(out[k][0], out[k][1]) - expected);
        energy += std::norm(expected);
      }
      require(std::sqrt(error / energy) < 2e-6);
    }
    auto fwd = fft_plan_dft_r2c_1d(n, real.data(), out, FFT_ESTIMATE);
    auto rev = fft_plan_dft_c2r_1d(n, out, roundtrip.data(), FFT_ESTIMATE);
    fft_execute(fwd);
    double real_error = 0, real_energy = 0;
    for (int k = 0; k <= n / 2; ++k) {
      std::complex<double> expected{};
      for (int i = 0; i < n; ++i)
        expected += static_cast<double>(real[i]) * std::polar(1.0, -2 * 3.14159265358979323846 * i * k / n);
      real_error += std::norm(std::complex<double>(out[k][0], out[k][1]) - expected);
      real_energy += std::norm(expected);
    }
    require(std::sqrt(real_error / real_energy) < 2e-6);
    fft_execute(rev);
    for (int i = 0; i < n; ++i) require(std::abs(roundtrip[i] / n - real[i]) < 2e-6);
    fft_destroy_plan(fwd); fft_destroy_plan(rev);
    require(session.ok());
  }
  if (sumi_world::kFftMaxComplexSize < 1024) {
    sumi_world::FftDspSession session;
    require(session.begin(nullptr, 0));
    // Reject an unsupported complex plan before touching either array.
    float sentinel[2]{1, 2};
    auto* data = reinterpret_cast<fft_complex*>(sentinel);
    fft_execute(fft_plan_dft_1d(1024, data, data, FFT_FORWARD, FFT_ESTIMATE));
    require(!session.ok() && sentinel[0] == 1 && sentinel[1] == 2);
  }
  std::puts("DSP FFT adapter checks passed");
}
