#include "world_activation.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <limits>

int main() {
  double maximum = 0;
  for (int i = 0; i <= 1000000; ++i) {
    const float x = -17.f + 34.f * i / 1000000;
    const double exact = static_cast<double>(x) / (1. + std::exp(-static_cast<double>(x)));
    maximum = std::max(maximum, std::fabs(static_cast<double>(sumi_silu_table(x)) - exact));
  }
  if (maximum > 3e-6 || !std::isnan(sumi_silu_table(std::numeric_limits<float>::quiet_NaN()))) std::abort();
  std::printf("SiLU cubic maximum absolute error %.9g\n", maximum);
}
