#pragma once
#include "world_math_tables.h"
#include "world_fast_math.h"

inline float sumi_lut_exp(float x) {
  if (!std::isfinite(x) || x < -80.f || x > 80.f) return std::exp(x);
  const float scaled = x * 1.4426950408889634f;
  const int k = static_cast<int>(scaled + (scaled >= 0 ? .5f : -.5f));
  const float r = (x - k * .693359375f) - k * (-.00021219444005469058f);
  const float index = r * 1477.3197218702985f + 512.f;
  if (index < 0 || index >= 1024.f) return std::exp(x);
  const int i = static_cast<int>(index);
  const float f = index - i;
  const float value = sumi_world_lut::kExp[i] + f * (sumi_world_lut::kExp[i + 1] - sumi_world_lut::kExp[i]);
  uint32_t bits = static_cast<uint32_t>(k + 127) << 23;
  const float scale = sumi_float_from_bits(bits);
  return value * scale;
}
inline float sumi_lut_log(float x) {
  if (!(x >= 1.17549435e-38f) || !std::isfinite(x)) return std::log(x);
  const uint32_t bits = sumi_float_bits(x);
  const int e = static_cast<int>(bits >> 23) - 127;
  const int i = (bits & 0x7fffff) >> 13;
  const float f = (bits & 8191) * (1.f / 8192.f);
  const float m = sumi_world_lut::kLog[i] + f * (sumi_world_lut::kLog[i + 1] - sumi_world_lut::kLog[i]);
  return e * .6931471805599453f + m;
}
inline float sumi_lut_sin(float x) {
  if (!std::isfinite(x) || std::fabs(x) > 100.f) return std::sin(x);
  float u = x * 325.94932345220167f;
  int i = static_cast<int>(u);
  if (u < i) --i;
  const float f = u - i;
  i &= 2047;
  return sumi_world_lut::kSine[i] + f * (sumi_world_lut::kSine[i + 1] - sumi_world_lut::kSine[i]);
}
inline float sumi_lut_cos(float x) {
  if (!std::isfinite(x) || std::fabs(x) > 100.f) return std::cos(x);
  float u = x * 325.94932345220167f;
  int i = static_cast<int>(u);
  if (u < i) --i;
  const float f = u - i;
  i = (i + 512) & 2047;
  return sumi_world_lut::kSine[i] + f * (sumi_world_lut::kSine[i + 1] - sumi_world_lut::kSine[i]);
}
inline void sumi_lut_sincos(float x, float* sine, float* cosine) {
  if (!std::isfinite(x) || std::fabs(x) > 100.f) {
    *sine = std::sin(x); *cosine = std::cos(x); return;
  }
  const float u = x * 325.94932345220167f;
  int i = static_cast<int>(u);
  if (u < i) --i;
  const float f = u - i;
  const int s = i & 2047, c = (i + 512) & 2047;
  *sine = sumi_world_lut::kSine[s] + f * (sumi_world_lut::kSine[s + 1] - sumi_world_lut::kSine[s]);
  *cosine = sumi_world_lut::kSine[c] + f * (sumi_world_lut::kSine[c + 1] - sumi_world_lut::kSine[c]);
}
inline float sumi_lut_pow(float x, float y) {
  if (!(x > 0) || !std::isfinite(y)) return std::pow(x, y);
  return sumi_lut_exp(y * (x == 10.f ? 2.302585092994046f : sumi_lut_log(x)));
}
inline float sumi_lut_sqrt(float x) { return sumi_fast_sqrt(x); }
inline float sumi_lut_log10(float x) { return sumi_lut_log(x) * .4342944819032518f; }
