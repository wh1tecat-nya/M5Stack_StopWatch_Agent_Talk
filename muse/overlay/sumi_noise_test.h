#pragma once
#include <stdbool.h>

typedef enum {
    SUMI_NOISE_IDLE, SUMI_NOISE_QUEUED, SUMI_NOISE_PAUSED,
    SUMI_NOISE_RESTORING, SUMI_NOISE_DONE, SUMI_NOISE_FAILED,
    SUMI_NOISE_RETRY
} sumi_noise_state_t;

bool sumi_noise_test_request(void);
void sumi_noise_test_cancel(void);
bool sumi_noise_test_active(void);
sumi_noise_state_t sumi_noise_test_state(void);
/* Called only by the voice task, while it owns idle audio. */
bool sumi_noise_test_step(bool shutting_down);
/* Board output-latch readback; does not measure the supply voltage. */
int sumi_audio_rail_enabled(void);
