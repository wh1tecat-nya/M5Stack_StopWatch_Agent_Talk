#pragma once
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace sumi_world {
// One producer publishes immutable prefixes. Only the audio owner calls step.
// The owner must finish before the producer releases the capture allocation.
class Playback {
 public:
  using Writer = bool (*)(void*, const int16_t*, std::size_t);
  static constexpr std::size_t kBlock = 320;
  void begin(const float* samples, std::size_t count, int64_t started_us,
             int64_t delay_us = 3000000) {
    samples_ = samples; count_ = count; started_ = started_us; delay_ = delay_us;
    cursor_ = 0; first_write_us = -1; underrun_blocks = clipped_samples = 0;
    published_.store(0); finished_.store(false); cancelled_.store(false); done_.store(false);
  }
  bool publish(std::size_t count) {
    if (count > count_ || count < published_.load()) return false;
    published_.store(count, std::memory_order_release);
    return true;
  }
  void finish() { finished_.store(true, std::memory_order_release); }
  void cancel() { cancelled_.store(true, std::memory_order_release); }
  bool cancelled() const { return cancelled_.load(std::memory_order_acquire); }
  bool done() const { return done_.load(std::memory_order_acquire); }
  std::size_t written() const { return cursor_; } // Read after done only.

  // The writer blocks for the hardware's output pacing. Before first output,
  // this returns without writing; the caller must yield while polling.
  void step(int64_t now_us, Writer writer, void* context) {
    if (done()) return;
    if (cancelled()) { done_.store(true, std::memory_order_release); return; }
    // Read completion before the prefix: observing finish also observes the
    // producer's final publication, so a stale prefix cannot cause rejection.
    const auto finished = finished_.load(std::memory_order_acquire);
    const auto ready = published_.load(std::memory_order_acquire);
    const auto need = std::min(kBlock, count_ - cursor_);
    if (!need) { done_.store(true, std::memory_order_release); return; }
    if (first_write_us < 0 && (now_us - started_ < delay_ || ready - cursor_ < need)) {
      if (finished && ready - cursor_ < need) {
        cancel(); done_.store(true, std::memory_order_release);
      }
      return;
    }
    int16_t pcm[kBlock]{};
    if (ready - cursor_ < need) {
      if (finished) {
        cancel(); done_.store(true, std::memory_order_release); return;
      }
      ++underrun_blocks;
      if (!writer(context, pcm, kBlock)) cancel();
      return;
    }
    for (std::size_t i = 0; i < need; ++i) {
      // Fixed headroom for observed model peaks; never normalize per block.
      const float sample = samples_[cursor_ + i] * 16384.0f;
      if (!std::isfinite(sample)) { cancel(); break; }
      if (sample < -32768.f || sample > 32767.f) ++clipped_samples;
      pcm[i] = static_cast<int16_t>(std::lround(std::clamp(sample, -32768.f, 32767.f)));
    }
    if (!cancelled()) {
      if (first_write_us < 0) first_write_us = now_us - started_;
      if (writer(context, pcm, need)) cursor_ += need;
      else cancel();
    }
    if (cancelled() || cursor_ == count_) done_.store(true, std::memory_order_release);
  }
  int64_t first_write_us = -1;
  unsigned underrun_blocks = 0, clipped_samples = 0;

 private:
  const float* samples_ = nullptr;
  std::size_t count_ = 0, cursor_ = 0;
  int64_t started_ = 0, delay_ = 0;
  std::atomic<std::size_t> published_{0};
  std::atomic<bool> finished_{false}, cancelled_{false}, done_{false};
};
} // namespace sumi_world
