#pragma once
#include <cmath>
#include "world_activation_tables.h"

inline float sumi_silu_table(float x) {
  if (!(x >= -16.f && x < 16.f)) return x / (1.f + std::exp(-x));
  const float position = x * 16.f;
  int cell = static_cast<int>(position);
  if (position < cell) --cell;
  const float t = position - cell;
  const float* c = sumi_activation::kCubic[cell + 256];
  return c[0] + t * (c[1] + t * (c[2] + t * c[3]));
}
