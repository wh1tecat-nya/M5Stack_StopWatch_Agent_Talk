#pragma once
#include <cmath>
#include <cstdint>

namespace sumi_world {
struct MathCounter { uint64_t calls = 0, cycles = 0; uint32_t samples = 0, remaining = 499; };
inline MathCounter math_profile[7];
inline uint32_t math_cycles() {
#ifdef ESP_PLATFORM
  uint32_t result;
  asm volatile("rsr.ccount %0" : "=a"(result));
  return result;
#else
  return 0;
#endif
}
template<int I, class F> inline float sampled_math(float x, F fn) {
  auto& counter = math_profile[I];
  ++counter.calls;
  if (--counter.remaining) return fn(x);
  counter.remaining = 499;
  const auto started = math_cycles();
#ifdef ESP_PLATFORM
  asm volatile("" : "+f"(x) : "a"(started) : "memory");
#endif
  const float result = fn(x);
  uint32_t ended;
#ifdef ESP_PLATFORM
  asm volatile("rsr.ccount %0" : "=a"(ended) : "f"(result) : "memory");
#else
  ended = 0;
#endif
  counter.cycles += static_cast<uint32_t>(ended - started);
  ++counter.samples;
  return result;
}
}
inline float sumi_profile_exp(float x) { return sumi_world::sampled_math<0>(x, [](float v) { return std::exp(v); }); }
inline float sumi_profile_log(float x) { return sumi_world::sampled_math<1>(x, [](float v) { return std::log(v); }); }
inline float sumi_profile_log10(float x) { return sumi_world::sampled_math<2>(x, [](float v) { return std::log10(v); }); }
inline float sumi_profile_pow(float x, float y) { return sumi_world::sampled_math<3>(x, [y](float v) { return std::pow(v, y); }); }
inline float sumi_profile_sqrt(float x) { return sumi_world::sampled_math<4>(x, [](float v) { return std::sqrt(v); }); }
inline float sumi_profile_cos(float x) { return sumi_world::sampled_math<5>(x, [](float v) { return std::cos(v); }); }
inline float sumi_profile_sin(float x) { return sumi_world::sampled_math<6>(x, [](float v) { return std::sin(v); }); }
