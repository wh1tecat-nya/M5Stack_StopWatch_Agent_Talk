#include "world_fast_math.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <limits>

void require(bool value) { if (!value) std::abort(); }
int main() {
  double exp_error = 0, log_error = 0, trig_error = 0, sqrt_error = 0;
  for (int i = 0; i <= 200000; ++i) {
    const float x = -80.f + 160.f * i / 200000;
    const float positive = std::exp(x);
    exp_error = std::max(exp_error, std::fabs(sumi_fast_exp(x) / static_cast<double>(std::exp(x)) - 1));
    log_error = std::max(log_error, std::fabs(static_cast<double>(sumi_fast_log(positive)) - std::log(positive)));
    trig_error = std::max(trig_error, std::fabs(static_cast<double>(sumi_fast_sin(x)) - std::sin(x)));
    trig_error = std::max(trig_error, std::fabs(static_cast<double>(sumi_fast_cos(x)) - std::cos(x)));
    sqrt_error = std::max(sqrt_error, std::fabs(sumi_fast_sqrt(positive) / static_cast<double>(std::sqrt(positive)) - 1));
  }
  require(exp_error < 3e-7 && log_error < 8e-6 && trig_error < 3e-7 && sqrt_error < 3e-7);
  require(std::isnan(sumi_fast_log(-1)) && std::isnan(sumi_fast_sqrt(-1)));
  require(std::isinf(sumi_fast_log(0)) && sumi_fast_sqrt(0) == 0);
  require(std::isinf(sumi_fast_exp(std::numeric_limits<float>::infinity())));
  std::printf("Bounded math max errors: exp_relative=%.9g log_abs=%.9g trig_abs=%.9g sqrt_relative=%.9g\n",
              exp_error, log_error, trig_error, sqrt_error);
}
