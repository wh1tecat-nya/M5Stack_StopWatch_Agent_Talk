#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

// Invoked only from the existing synthesis worker in an explicit diagnostic build.
bool sumi_world_bench_run(bool (*keep_running)(uint32_t), uint32_t generation);

#ifdef __cplusplus
extern "C" {
#endif
// Diagnostic-only ownership: the existing voice task remains the sole writer.
bool sumi_world_bench_audio_reserve(void);
bool sumi_world_bench_audio_delay(unsigned milliseconds);
void sumi_world_bench_audio_release(void);
bool sumi_world_bench_audio_active(void);
void sumi_world_bench_audio_step(bool stop, bool audio_on,
    bool (*write)(const int16_t*, size_t), void (*quiet)(void));
#ifdef __cplusplus
}
#endif
