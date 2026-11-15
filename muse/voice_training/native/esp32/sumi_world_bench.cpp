#include "sumi_world_bench.h"
#include "world_envelope.h"
#include "world_stream.h"
#include "world_playback.h"
#ifdef SUMI_WORLD_FFT_DSP
#include "world_fft_dsp.h"
#endif
#ifdef SUMI_WORLD_MATH_PROFILE
#include "world_math_profile.h"
#endif
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <atomic>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_private/esp_clk.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#ifdef SUMI_WORLD_ESP_DSP
#include "dspm_mult.h"
#endif

extern const uint8_t weight_start[] asm("_binary_world_weights_bin_start");
extern const uint8_t weight_end[] asm("_binary_world_weights_bin_end");
extern const uint8_t fixture_start[] asm("_binary_world_fixture_bin_start");
extern const uint8_t fixture_end[] asm("_binary_world_fixture_bin_end");
extern const uint8_t reference_features[] asm("_binary_world_reference_features_bin_start");
extern const uint8_t reference_wave[] asm("_binary_world_reference_wave_bin_start");

namespace {
constexpr char kTag[] = "sumi_world_bench";
#ifndef SUMI_WORLD_BENCH_CHUNK
#define SUMI_WORLD_BENCH_CHUNK 64
#endif
#ifndef SUMI_WORLD_BENCH_SAMPLES
#define SUMI_WORLD_BENCH_SAMPLES 22050
#endif
#ifndef SUMI_WORLD_BENCH_WAVE_PRIORITY
#define SUMI_WORLD_BENCH_WAVE_PRIORITY 3
#endif
#ifndef SUMI_WORLD_BENCH_FRAMES
#define SUMI_WORLD_BENCH_FRAMES 993
#endif
constexpr std::size_t kFrames = SUMI_WORLD_BENCH_FRAMES, kSamples = SUMI_WORLD_BENCH_SAMPLES, kChunk = SUMI_WORLD_BENCH_CHUNK;
constexpr std::size_t kWindow = kChunk + 2 * sumi_world::kHaloFrames + 4;
#ifdef SUMI_WORLD_BENCH_PLAYBACK
enum AudioState { Idle, Preparing, Reserved, Playing };
std::atomic<int> audio_state{Idle};
std::atomic<bool> audio_stop{false}, audio_done{false};
std::atomic<unsigned> audio_delay_ms{3000};
sumi_world::Playback player;
#endif
#ifdef SUMI_WORLD_ESP_DSP
constexpr std::size_t kWeightPrefix = 8;
#else
constexpr std::size_t kWeightPrefix = 0;
#endif
struct Output {
  bool (*keep)(uint32_t);
  uint32_t generation;
  int64_t deadline, started, first_pcm = -1;
  double square = 0, peak = 0, error_square = 0, reference_square = 0, max_error = 0;
  std::size_t count = 0;
  float* capture = nullptr;
  int64_t required_start_us = 0;
};
bool collect(void* context, const float* data, std::size_t n) {
  auto& out = *static_cast<Output*>(context);
  if (!out.keep(out.generation) || esp_timer_get_time() >= out.deadline) return false;
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  if (audio_stop.load()) return false;
#endif
  if (out.first_pcm < 0) out.first_pcm = esp_timer_get_time() - out.started;
  out.required_start_us = std::max(out.required_start_us, esp_timer_get_time() - out.started -
      static_cast<int64_t>(out.count) * 1000000 / sumi_world::kWorldSampleRate);
  if (!out.capture || out.count + n > kSamples) return false;
  std::copy(data, data + n, out.capture + out.count);
  out.count += n;
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  if (!player.publish(out.count)) return false;
#endif
  // Permit idle/network tasks to run and allow generation cancellation.
  vTaskDelay(1);
  return true;
}
void compare_capture(Output& out) {
  // Verification uses software double arithmetic and flash reads. Perform it
  // after the synthesis timer, retaining every emitted sample for comparison.
  for (std::size_t i = 0; i < out.count; ++i) {
    const double value = out.capture[i];
    const double expected = reinterpret_cast<const float*>(reference_wave)[i];
    const double delta = value - expected;
    out.square += value * value;
    out.peak = std::max(out.peak, std::fabs(value));
    out.error_square += delta * delta;
    out.reference_square += expected * expected;
    out.max_error = std::max(out.max_error, std::fabs(delta));
    if (!(i % 4096)) vTaskDelay(1);
  }
}
#ifdef SUMI_WORLD_BENCH_PIPELINE
struct Pipeline {
  float* physical = nullptr;
  Output* output = nullptr;
  sumi_world::SynthesisStats stats{};
  std::atomic<std::size_t> ready{0};
  std::atomic<bool> cancel{false}, done{false};
  TaskHandle_t waiter = nullptr;
  int64_t started = 0, finished = 0, wait_us = 0;
  bool result = false;
  static bool wait(void* context, std::size_t needed) {
    auto& job = *static_cast<Pipeline*>(context);
    const auto begin = esp_timer_get_time();
    while (job.ready.load(std::memory_order_acquire) < needed) {
      if (job.cancel.load() || !job.output->keep(job.output->generation) ||
          esp_timer_get_time() >= job.output->deadline) return false;
      vTaskDelay(1);
    }
    job.wait_us += esp_timer_get_time() - begin;
    return !job.cancel.load();
  }
  static void work(void* context) {
    auto& job = *static_cast<Pipeline*>(context);
    job.started = esp_timer_get_time();
    job.result = sumi_world::synthesize_stream(job.physical, kFrames * 64, kFrames, kSamples,
        std::min<std::size_t>(512, sumi_world::kWorldFftSize), collect, job.output, &job.stats,
        nullptr, 0, wait, &job);
    job.finished = esp_timer_get_time();
    ESP_LOGI(kTag, "wave_stack_unused=%u", (unsigned)uxTaskGetStackHighWaterMark(nullptr));
    if (!job.result) job.cancel.store(true);
    const auto waiter = job.waiter;
    job.done.store(true, std::memory_order_release);
    xTaskNotifyGive(waiter);
    vTaskDeleteWithCaps(nullptr);
  }
};
#endif
}

extern "C" bool sumi_world_bench_audio_reserve(void) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  int expected = Idle;
  if (!audio_state.compare_exchange_strong(expected, Preparing)) return false;
  audio_stop.store(false); audio_done.store(false);
  audio_state.store(Reserved, std::memory_order_release);
#endif
  return true;
}
extern "C" bool sumi_world_bench_audio_delay(unsigned milliseconds) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  if (milliseconds < 1000 || milliseconds > 5000 || sumi_world_bench_audio_active()) return false;
  audio_delay_ms.store(milliseconds);
  return true;
#else
  (void)milliseconds;
  return false;
#endif
}
extern "C" void sumi_world_bench_audio_release(void) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  audio_state.store(Idle, std::memory_order_release);
#endif
}
extern "C" bool sumi_world_bench_audio_active(void) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  return audio_state.load(std::memory_order_acquire) != Idle;
#else
  return false;
#endif
}
extern "C" void sumi_world_bench_audio_step(bool stop, bool audio_on,
    bool (*write)(const int16_t*, size_t), void (*quiet)(void)) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  if (stop || !audio_on) audio_stop.store(true);
  if (audio_state.load(std::memory_order_acquire) != Playing || audio_done.load()) return;
  if (audio_stop.load()) player.cancel();
  auto writer = [](void* context, const int16_t* data, size_t count) {
    return (*static_cast<bool (**)(const int16_t*, size_t)>(context))(data, count);
  };
  player.step(esp_timer_get_time(), writer, &write);
  if (player.done()) {
    quiet();
    if (player.cancelled()) audio_stop.store(true);
    audio_done.store(true, std::memory_order_release);
  }
#else
  (void)stop; (void)audio_on; (void)write; (void)quiet;
#endif
}

static bool bench_run_current_task(bool (*keep)(uint32_t), uint32_t generation) {
  const auto weight_bytes = static_cast<std::size_t>(weight_end - weight_start) - kWeightPrefix;
  const auto input_bytes = static_cast<std::size_t>(fixture_end - fixture_start);
  if (!keep || weight_bytes != sumi_world::kWeightFloats * 4 || input_bytes != kFrames * 32 * 4 ||
      (reinterpret_cast<uintptr_t>(weight_start) & 3) || (reinterpret_cast<uintptr_t>(fixture_start) & 3)) {
    ESP_LOGE(kTag, "invalid embedded model/fixture"); return false;
  }
  const std::size_t scratch_count = sumi_world::EnvelopePredictor::workspace_floats(kWindow);
  const std::size_t arena_count = scratch_count + kWindow * 96 + kFrames * 64 + kSamples;
  const std::size_t total_arena_count = arena_count + sumi_world::kWeightFloats + 2;
  const auto before_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
  const auto before_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  if (before_psram < total_arena_count * 4 + 700000 || before_internal < 16000 + sumi_world::kMatrixWorkspaceFloats * 4 ||
      heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM) < total_arena_count * 4) {
    ESP_LOGE(kTag, "not enough free memory: psram=%u internal=%u arena=%u",
             (unsigned)before_psram, (unsigned)before_internal, (unsigned)(total_arena_count * 4));
    return false;
  }
  esp_pm_lock_handle_t clock_lock = nullptr;
  if (esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "world_bench", &clock_lock) != ESP_OK) return false;
  if (esp_pm_lock_acquire(clock_lock) != ESP_OK) { esp_pm_lock_delete(clock_lock); return false; }
  const bool monitoring = heap_caps_monitor_local_minimum_free_size_start() == ESP_OK;
  auto* arena = static_cast<float*>(heap_caps_aligned_alloc(16, total_arena_count * 4, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  auto* matrix = static_cast<float*>(heap_caps_aligned_alloc(16, sumi_world::kMatrixWorkspaceFloats * 4,
                                                           MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (!arena || !matrix) {
    heap_caps_free(arena); heap_caps_free(matrix);
    if (monitoring) heap_caps_monitor_local_minimum_free_size_stop();
    esp_pm_lock_release(clock_lock); esp_pm_lock_delete(clock_lock); return false;
  }
  float* local_input = arena + scratch_count;
  float* local_output = local_input + kWindow * 32;
  float* physical = local_output + kWindow * 64;
  const auto* latent = reinterpret_cast<const float*>(fixture_start);
  // Flash is DIO; repeatedly reading model weights there dominates inference.
  // Retain the exact FP32 values in PSRAM, leaving internal SRAM for tiles.
  float* ram_weights = arena + arena_count + 2;
  std::copy(reinterpret_cast<const float*>(weight_start + kWeightPrefix),
            reinterpret_cast<const float*>(weight_start + kWeightPrefix) + sumi_world::kWeightFloats, ram_weights);
#ifdef SUMI_WORLD_ESP_DSP
  std::fill(matrix, matrix + sumi_world::kMatrixWorkspaceFloats, .01f);
  const auto matrix_started = esp_timer_get_time();
  for (int i = 0; i < 100; ++i) dspm_mult_f32(matrix, matrix + 512, matrix + 1536, 4, 128, 8);
  ESP_LOGI(kTag, "matrix_kernel_409600_macs=%.6fs", (esp_timer_get_time() - matrix_started) / 1e6);
#endif
  sumi_world::EnvelopePredictor model;
  bool ok = model.bind(ram_weights, weight_bytes / 4);
  const auto started = esp_timer_get_time();
  Output out{keep, generation, started + 120000000, started};
  out.capture = physical + kFrames * 64;
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  player.begin(out.capture, kSamples, started, static_cast<int64_t>(audio_delay_ms.load()) * 1000);
  ESP_LOGI(kTag, "playback_preroll_ms=%u", audio_delay_ms.load());
  audio_state.store(Playing, std::memory_order_release);
#endif
  ESP_LOGI(kTag, "start: CPU=%u core=%d frames=%u neural_duration=%.3fs wave_duration=%.3fs arena=%u model_state=%u",
           (unsigned)(esp_clk_cpu_freq() / 1000000), xPortGetCoreID(), (unsigned)kFrames, (kFrames - 1) / 200.0,
           kSamples / static_cast<double>(sumi_world::kWorldSampleRate), (unsigned)(total_arena_count * 4), (unsigned)sizeof(model));
  ESP_LOGI(kTag, "wave_real_bytes=%u chunk_frames=%u", (unsigned)sizeof(sumi_world::WorldReal), (unsigned)kChunk);
#ifdef SUMI_WORLD_BENCH_PIPELINE
  Pipeline pipeline;
  pipeline.physical = physical;
  pipeline.output = &out;
  pipeline.waiter = xTaskGetCurrentTaskHandle();
  const bool worker_created = xTaskCreatePinnedToCoreWithCaps(Pipeline::work, "world_wave", 3072,
      &pipeline, SUMI_WORLD_BENCH_WAVE_PRIORITY, nullptr, 1, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) == pdPASS;
  ok = ok && worker_created;
#endif
  for (std::size_t begin = 0; ok && begin < kFrames; begin += kChunk) {
#ifdef SUMI_WORLD_BENCH_PLAYBACK
    if (audio_stop.load()) { ok = false; break; }
#endif
#ifdef SUMI_WORLD_BENCH_PIPELINE
    if (pipeline.cancel.load()) { ok = false; break; }
#endif
    if (!keep(generation) || esp_timer_get_time() >= out.deadline) { ok = false; break; }
    const auto end = std::min(kFrames, begin + kChunk);
    auto left = begin > sumi_world::kHaloFrames ? begin - sumi_world::kHaloFrames : 0;
    auto right = std::min(kFrames, end + sumi_world::kHaloFrames);
    // Add real context, never synthetic frames, to align every matrix row.
    const auto extra = (4 - (right - left) % 4) % 4;
    if (right + extra <= kFrames) right += extra;
    else if (left >= extra) left -= extra;
    const auto width = right - left;
    for (std::size_t c = 0; c < 32; ++c)
      std::copy(latent + c * kFrames + left, latent + c * kFrames + right, local_input + c * width);
    ok = model.predict(local_input, width * 32, width, local_output, width * 64, arena, scratch_count,
                       matrix, sumi_world::kMatrixWorkspaceFloats) &&
        model.to_physical(local_output, width * 64, width);
    if (ok) for (std::size_t c = 0; c < 64; ++c)
      std::copy(local_output + c * width + begin - left, local_output + c * width + end - left,
                physical + c * kFrames + begin);
#ifdef SUMI_WORLD_BENCH_PIPELINE
    if (ok) pipeline.ready.store(end, std::memory_order_release);
#endif
    vTaskDelay(1);
  }
  const auto predicted = esp_timer_get_time();
  ESP_LOGI(kTag, "prediction done ok=%d elapsed=%.3fs", ok, (predicted - started) / 1e6);
  const auto model_profile = model.profile();
  ESP_LOGI(kTag, "prediction_profile dense=%.3fs depth=%.3fs activation=%.3fs macs=%llu",
           model_profile.dense_us / 1e6, model_profile.depth_us / 1e6, model_profile.activation_us / 1e6, model_profile.dense_macs);
  ESP_LOGI(kTag, "matrix_profile packing=%.3fs multiply=%.3fs", model_profile.pack_us / 1e6, model_profile.matmul_us / 1e6);
  ESP_LOGI(kTag, "copy_profile inputs=%.3fs weights=%.3fs", model_profile.input_us / 1e6, model_profile.weights_us / 1e6);
  double max_sp = 0, max_ap = 0, max_cents = 0;
  unsigned voicing_differences = 0;
  const auto* expected = reinterpret_cast<const float*>(reference_features);
  if (ok) for (std::size_t c = 0; c < 64; ++c) for (std::size_t t = 0; t < kFrames; ++t) {
    const auto i = c * kFrames + t;
    const double delta = std::fabs(static_cast<double>(physical[i]) - expected[i]);
    if (c < 60) max_sp = std::max(max_sp, delta);
    else if (c < 62) max_ap = std::max(max_ap, delta);
    else if (c == 62) {
      if ((physical[i] > 0) != (expected[i] > 0)) ++voicing_differences;
      else if (expected[i] > 0) max_cents = std::max(max_cents,
          std::fabs(1200 * std::log2(static_cast<double>(physical[i]) / expected[i])));
    }
  }
  ESP_LOGI(kTag, "features max_sp=%.9g max_ap=%.9g max_f0_cents=%.9g voicing_differences=%u",
           max_sp, max_ap, max_cents, voicing_differences);
  // Neural prediction is complete. Release its larger tile arena so the
  // waveform stage can allocate just the FFT scratch and cache its filters
  // in internal RAM. No data from the tile survives into synthesis.
  heap_caps_free(matrix);
  matrix = nullptr;
  const auto wave_started = esp_timer_get_time();
#ifdef SUMI_WORLD_MATH_PROFILE
  for (auto& counter : sumi_world::math_profile) counter = {};
#endif
  sumi_world::SynthesisStats stats;
  int64_t wave_elapsed = 0;
#ifdef SUMI_WORLD_BENCH_PIPELINE
  if (!ok) pipeline.cancel.store(true);
  if (worker_created) {
    while (!pipeline.done.load(std::memory_order_acquire)) ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    ok = ok && pipeline.result;
    stats = pipeline.stats;
    wave_elapsed = pipeline.finished - pipeline.started - pipeline.wait_us;
  }
  ESP_LOGI(kTag, "pipeline=1 wave_feature_wait=%.3fs", pipeline.wait_us / 1e6);
#else
  if (ok) ok = sumi_world::synthesize_stream(physical, kFrames * 64, kFrames, kSamples,
      std::min<std::size_t>(512, sumi_world::kWorldFftSize), collect, &out, &stats);
  wave_elapsed = esp_timer_get_time() - wave_started;
#endif
  (void)wave_started;
  const auto finished = esp_timer_get_time();
#ifdef SUMI_WORLD_BENCH_PLAYBACK
  if (!ok || out.count != kSamples) player.cancel();
  player.finish();
  while (!audio_done.load(std::memory_order_acquire)) {
    if (!keep(generation) || esp_timer_get_time() >= out.deadline) audio_stop.store(true);
    vTaskDelay(1);
  }
  ok = ok && !player.cancelled() && player.written() == kSamples;
  ESP_LOGI(kTag, "speaker played=%d first_write=%.3fs underrun_blocks=%u clipped=%u samples=%u duration=%.3fs",
           player.written() > 0, player.first_write_us / 1e6, player.underrun_blocks,
           player.clipped_samples, (unsigned)player.written(), (esp_timer_get_time() - started) / 1e6);
#endif
  compare_capture(out);
  const auto low_psram = monitoring ? heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM) : 0;
  const auto low_internal = monitoring ? heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) : 0;
  heap_caps_free(arena);
  heap_caps_free(matrix);
  if (monitoring) heap_caps_monitor_local_minimum_free_size_stop();
  esp_pm_lock_release(clock_lock); esp_pm_lock_delete(clock_lock);
  ESP_LOGI(kTag, "result ok=%d neural=%.3fs wave=%.3fs total=%.3fs neural_rtf=%.3f wave_rtf=%.3f first_pcm=%.3fs samples=%u rms=%.8f peak=%.8f",
           ok, (predicted - started) / 1e6, wave_elapsed / 1e6, (finished - started) / 1e6,
           (predicted - started) / ((kFrames - 1) * 5000.0), wave_elapsed / (kSamples / static_cast<double>(sumi_world::kWorldSampleRate) * 1e6),
           out.first_pcm / 1e6, (unsigned)out.count,
           out.count ? std::sqrt(out.square / out.count) : 0, out.peak);
  ESP_LOGI(kTag, "wave_error relative_l2=%.9g max_abs=%.9g", std::sqrt(out.error_square / std::max(1e-20, out.reference_square)), out.max_error);
  ESP_LOGI(kTag, "reference_audit=after_timer capture_bytes=%u", unsigned(kSamples * sizeof(float)));
  ESP_LOGI(kTag, "playback_schedule simulated=1 minimum_start_delay=%.3fs", out.required_start_us / 1e6);
  ESP_LOGI(kTag, "profile decode=%.3fs response=%.3fs phase=%.3fs pulses=%u frames_decoded=%u",
           stats.decode_us / 1e6, stats.response_us / 1e6, stats.phase_us / 1e6,
           (unsigned)stats.pulses, (unsigned)stats.decoded_frames);
  ESP_LOGI(kTag, "internal_wave_state=%u", (unsigned)stats.internal_state_bytes);
  ESP_LOGI(kTag, "frame_filters stride=%d frames=%u", SUMI_WORLD_FILTER_STRIDE, (unsigned)stats.filter_frames);
#ifdef SUMI_WORLD_FFT_DSP
  const auto fft_profile = sumi_world::fft_dsp_profile();
  ESP_LOGI(kTag, "fft_profile calls=%llu total=%.3fs transform=%.3fs", fft_profile.calls,
           fft_profile.total_us / 1e6, fft_profile.transform_us / 1e6);
#endif
#ifdef SUMI_WORLD_MATH_PROFILE
  const char* math_names[] = {"exp", "log", "log10", "pow", "sqrt", "cos", "sin"};
  for (int i = 0; i < 7; ++i) {
    const auto& c = sumi_world::math_profile[i];
    ESP_LOGI(kTag, "math_profile %s calls=%llu samples=%u estimated_seconds=%.6f", math_names[i], c.calls,
             (unsigned)c.samples, c.samples ? static_cast<double>(c.cycles) * c.calls / c.samples / 240000000. : 0.);
  }
#endif
  ESP_LOGI(kTag, "memory monitor=%d psram_before=%u low=%u after=%u internal_before=%u low=%u after=%u wrapper=%u stack_unused=%u",
           monitoring, (unsigned)before_psram, (unsigned)low_psram,
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), (unsigned)before_internal,
           (unsigned)low_internal, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)stats.wrapper_state_bytes, (unsigned)uxTaskGetStackHighWaterMark(nullptr));
  return ok && out.count == kSamples;
}

bool sumi_world_bench_run(bool (*keep)(uint32_t), uint32_t generation) {
#ifdef SUMI_WORLD_BENCH_INTERNAL_STACK
#ifndef SUMI_WORLD_BENCH_STACK_BYTES
#define SUMI_WORLD_BENCH_STACK_BYTES 12288
#endif
  struct Job {
    bool (*keep)(uint32_t);
    uint32_t generation;
    TaskHandle_t waiter;
    bool result;
  } job{keep, generation, xTaskGetCurrentTaskHandle(), false};
  // Diagnostic-only: retain the existing production worker and move just this
  // run to an internal stack. The parent stays alive until the result is ready.
  auto entry = [](void* argument) {
    auto& job = *static_cast<Job*>(argument);
    ESP_LOGI(kTag, "worker_stack=internal bytes=%u", unsigned(SUMI_WORLD_BENCH_STACK_BYTES));
    job.result = bench_run_current_task(job.keep, job.generation);
    xTaskNotifyGive(job.waiter);
    vTaskDeleteWithCaps(nullptr);
  };
  if (xTaskCreatePinnedToCoreWithCaps(entry, "world_internal", SUMI_WORLD_BENCH_STACK_BYTES, &job, 3,
                                    nullptr, 0, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) != pdPASS) {
    ESP_LOGE(kTag, "cannot allocate internal benchmark stack");
    return false;
  }
  ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
  return job.result;
#else
  ESP_LOGI(kTag, "worker_stack=existing");
  return bench_run_current_task(keep, generation);
#endif
}
