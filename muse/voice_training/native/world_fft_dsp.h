#pragma once
#include <cstddef>

namespace sumi_world {
// Real 1024-point transforms use a 512-point complex transform. The compact
// configuration retains that path and the codec's 512-point complex IDCT.
#ifdef SUMI_WORLD_COMPACT_WORKSPACE
constexpr int kFftMaxComplexSize = 512;
#else
constexpr int kFftMaxComplexSize = 1024;
#endif
constexpr std::size_t kFftWorkspaceFloats = 3 * kFftMaxComplexSize;
struct FftProfile { unsigned long long calls = 0, total_us = 0, transform_us = 0; };
FftProfile fft_dsp_profile();
bool fft_dsp_ok();
// WORLD invokes its plans sequentially. One session owns the shared FFT work
// area; reject concurrent/nested sessions rather than corrupting another run.
class FftDspSession {
 public:
  bool begin(float* workspace, std::size_t count);
  bool ok() const;
  ~FftDspSession();
  FftDspSession() = default;
  FftDspSession(const FftDspSession&) = delete;
  FftDspSession& operator=(const FftDspSession&) = delete;
 private:
  bool acquired_ = false;
  float* owned_ = nullptr;
};
}
