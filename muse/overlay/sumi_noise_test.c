#include "sumi_noise_test.h"
#include "muse_audio.h"
#include "esp_timer.h"
#include "esp_log.h"
#include <stdatomic.h>
#include <stdint.h>

static atomic_int state;
static atomic_bool cancel;
static int64_t deadline, retry_at;
static bool off_verified;

sumi_noise_state_t sumi_noise_test_state(void) { return atomic_load(&state); }
bool sumi_noise_test_active(void) {
    int s = atomic_load(&state);
    return s == SUMI_NOISE_QUEUED || s == SUMI_NOISE_PAUSED ||
           s == SUMI_NOISE_RESTORING || s == SUMI_NOISE_RETRY;
}
bool sumi_noise_test_request(void) {
    if (sumi_noise_test_active()) return false;
    atomic_store(&cancel, false);
    atomic_store(&state, SUMI_NOISE_QUEUED);
    return true;
}
void sumi_noise_test_cancel(void) { atomic_store(&cancel, true); }

bool sumi_noise_test_step(bool shutting_down) {
    if (!sumi_noise_test_active()) return false;
    int64_t now = esp_timer_get_time();
    int s = atomic_load(&state);
    if (s == SUMI_NOISE_QUEUED) {
        if (shutting_down || atomic_load(&cancel)) {
            atomic_store(&state, SUMI_NOISE_DONE);
            return false;
        }
        muse_audio_quiet();
        muse_audio_power(false);
        off_verified = sumi_audio_rail_enabled() == 0;
        deadline = esp_timer_get_time() + 20000000;
        retry_at = 0;
        atomic_store(&state, off_verified ? SUMI_NOISE_PAUSED : SUMI_NOISE_RESTORING);
        ESP_LOGI("sumi_noise", "audio pause: off verified=%d", off_verified);
        if (off_verified) return true;
    }
    if (atomic_load(&state) == SUMI_NOISE_PAUSED) {
        if (!shutting_down && !atomic_load(&cancel) && now < deadline) return true;
        atomic_store(&state, SUMI_NOISE_RESTORING);
    }
    if (!shutting_down && !atomic_load(&cancel) && now < retry_at) return true;
    muse_audio_power(true);
    bool restored = muse_audio_is_on() && sumi_audio_rail_enabled() == 1;
    if (restored || shutting_down) {
        ESP_LOGI("sumi_noise", "audio resume: verified=%d", restored);
        atomic_store(&state, restored && off_verified ? SUMI_NOISE_DONE : SUMI_NOISE_FAILED);
        atomic_store(&cancel, false);
        return false;
    }
    /* Hold capture/turns until recovery. Keep retrying even if the UI closes. */
    atomic_store(&cancel, false);
    retry_at = esp_timer_get_time() + 1000000;
    atomic_store(&state, SUMI_NOISE_RETRY);
    ESP_LOGW("sumi_noise", "audio resume failed; retrying");
    return true;
}
