#include "world_playback.h"
#include <cassert>
#include <limits>
#include <vector>

namespace {
bool write(void* context, const int16_t* data, std::size_t count) {
  auto& output = *static_cast<std::vector<int16_t>*>(context);
  output.insert(output.end(), data, data + count);
  return true;
}
bool fail(void*, const int16_t*, std::size_t) { return false; }
}
int main() {
  std::vector<float> source(800, .5f);
  std::vector<int16_t> output;
  sumi_world::Playback p;
  p.begin(source.data(), source.size(), 100);
  assert(!p.publish(801));
  assert(p.publish(320));
  assert(!p.publish(319));
  p.step(3000099, write, &output);
  assert(output.empty());
  p.step(3000100, write, &output);
  assert(output.size() == 320 && p.first_write_us == 3000000);
  assert(output.front() == 8192);
  p.step(3020100, write, &output);
  assert(p.underrun_blocks == 1 && output.size() == 640 && output.back() == 0);
  assert(p.publish(800)); p.finish();
  p.step(3040100, write, &output); p.step(3060100, write, &output);
  assert(p.done() && !p.cancelled() && p.written() == 800 && output.size() == 1120);
  p.step(3080100, write, &output); assert(output.size() == 1120);
  p.begin(source.data(), source.size(), 0, 0); p.publish(320); p.finish();
  p.step(0, write, &output); p.step(20000, write, &output);
  assert(p.done() && p.cancelled());
  p.begin(source.data(), source.size(), 0, 0); p.publish(800);
  p.step(0, fail, nullptr); assert(p.done() && p.cancelled());
  p.begin(source.data(), source.size(), 0, 0); p.cancel();
  p.step(0, write, &output); assert(p.done() && p.written() == 0);
  source[0] = std::numeric_limits<float>::quiet_NaN();
  p.begin(source.data(), source.size(), 0, 0); p.publish(800);
  p.step(0, write, &output); assert(p.done() && p.cancelled() && p.written() == 0);
}
