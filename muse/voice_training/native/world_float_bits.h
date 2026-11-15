#pragma once
#include <cstdint>
#include <cstring>

// ESP-IDF disables some libc builtins. An ordinary memcpy of four bytes can
// consequently become a function call in every spectrum bin. bit_cast keeps
// the same representation without type-punning undefined behavior.
inline std::uint32_t sumi_float_bits(float value) {
#if defined(__has_builtin)
#if __has_builtin(__builtin_bit_cast)
  return __builtin_bit_cast(std::uint32_t, value);
#endif
#endif
  std::uint32_t result;
  std::memcpy(&result, &value, sizeof(result));
  return result;
}
inline float sumi_float_from_bits(std::uint32_t value) {
#if defined(__has_builtin)
#if __has_builtin(__builtin_bit_cast)
  return __builtin_bit_cast(float, value);
#endif
#endif
  float result;
  std::memcpy(&result, &value, sizeof(result));
  return result;
}
