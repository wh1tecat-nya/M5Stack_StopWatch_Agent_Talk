#pragma once
// Explicit bounded approximations, not compiler -ffast-math. Exceptional and
// out-of-domain inputs retain libm behavior. Adoption requires waveform audits.
#include <cmath>
#include <cstdint>
#include <cstring>
#include "world_float_bits.h"

inline float sumi_fast_exp(float x) {
  if (!std::isfinite(x) || x < -80.f || x > 80.f) return std::exp(x);
  const float scaled = x * 1.4426950408889634f;
  const int k = static_cast<int>(scaled + (scaled >= 0 ? .5f : -.5f));
  const float r = (x - k * .693359375f) - k * (-.00021219444005469058f);
  float p = 1.f / 5040.f;
  p = 1.f / 720.f + r * p;
  p = 1.f / 120.f + r * p;
  p = 1.f / 24.f + r * p;
  p = 1.f / 6.f + r * p;
  p = .5f + r * p;
  p = 1.f + r * p;
  p = 1.f + r * p;
  const uint32_t bits = static_cast<uint32_t>(k + 127) << 23;
  const float scale = sumi_float_from_bits(bits);
  return p * scale;
}

inline float sumi_fast_log(float x) {
  if (!(x >= 1.17549435e-38f) || !std::isfinite(x)) return std::log(x);
  uint32_t bits = sumi_float_bits(x);
  int e = static_cast<int>(bits >> 23) - 127;
  bits = (bits & 0x7fffff) | 0x3f800000;
  float m = sumi_float_from_bits(bits);
  if (m > 1.41421356237f) { m *= .5f; ++e; }
  const float z = (m - 1.f) / (m + 1.f), z2 = z * z;
  const float p = 1.f + z2 * (1.f / 3.f + z2 * (1.f / 5.f + z2 * (1.f / 7.f + z2 / 9.f)));
  return e * .6931471805599453f + 2.f * z * p;
}

inline float sumi_fast_angle(float x) {
  const float q = x * .15915494309189535f;
  const int k = static_cast<int>(q + (q >= 0 ? .5f : -.5f));
  return (x - k * 6.28125f) - k * .001935307179586477f;
}
inline float sumi_fast_sin(float x) {
  if (!std::isfinite(x) || std::fabs(x) > 100.f) return std::sin(x);
  float r = sumi_fast_angle(x);
  if (r > 1.57079632679f) r = 3.14159265359f - r;
  else if (r < -1.57079632679f) r = -3.14159265359f - r;
  const float r2 = r * r;
  return r * (1.f + r2 * (-1.f / 6.f + r2 * (1.f / 120.f + r2 * (-1.f / 5040.f +
      r2 * (1.f / 362880.f - r2 / 39916800.f)))));
}
inline float sumi_fast_cos(float x) {
  if (!std::isfinite(x) || std::fabs(x) > 100.f) return std::cos(x);
  float r = std::fabs(sumi_fast_angle(x)), sign = 1;
  if (r > 1.57079632679f) { r = 3.14159265359f - r; sign = -1; }
  const float r2 = r * r;
  return sign * (1.f + r2 * (-.5f + r2 * (1.f / 24.f + r2 * (-1.f / 720.f +
      r2 * (1.f / 40320.f + r2 * (-1.f / 3628800.f + r2 / 479001600.f))))));
}
inline float sumi_fast_sqrt(float x) {
  if (!(x >= 1.17549435e-38f) || x > 1e30f || !std::isfinite(x)) return std::sqrt(x);
  uint32_t bits = sumi_float_bits(x); bits = 0x5f375a86u - (bits >> 1);
  float y = sumi_float_from_bits(bits);
  const float half = .5f * x;
  y *= 1.5f - half * y * y;
  y *= 1.5f - half * y * y;
  y *= 1.5f - half * y * y;
  return x * y;
}
inline float sumi_fast_pow(float x, float y) {
  if (!(x > 0) || !std::isfinite(y)) return std::pow(x, y);
  return sumi_fast_exp(y * sumi_fast_log(x));
}
inline float sumi_fast_log10(float x) { return sumi_fast_log(x) * .4342944819032518f; }
