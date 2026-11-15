#pragma once
#include <stdint.h>
#include <stdbool.h>
/* 63-tap Blackman-windowed sinc, cutoff 7 kHz at 22.05 kHz.
 * Q15 coefficients sum to 32768. Filter before reducing to 16 kHz so
 * frequencies above 8 kHz cannot fold back into the audible speech band. */
#define SUMI_FIR_TAPS 63
static const int16_t sumi_fir[SUMI_FIR_TAPS] = {
0,0,1,-2,-3,10,-6,-16,31,-4,-52,64,20,-127,99,98,-254,107,273,-431,28,603,-640,-245,1191,-840,-978,2418,-987,-3845,9467,20808,9467,-3845,-987,2418,-978,-840,1191,-245,-640,603,28,-431,273,107,-254,98,99,-127,20,64,-52,-4,31,-16,-6,10,-3,-2,1,0,0
};
typedef struct {
    uint32_t phase;
    int32_t previous;
    int16_t history[SUMI_FIR_TAPS];
    unsigned cursor;
    bool initialized;
} sumi_resampler_t;
static inline bool sumi_resample_one(sumi_resampler_t *s, int16_t sample, int16_t *out) {
    if (!s->initialized) {
        for (unsigned i = 0; i < SUMI_FIR_TAPS; ++i) s->history[i] = sample;
        s->previous = sample; s->initialized = true;
    }
    s->history[s->cursor] = sample;
    unsigned at = s->cursor;
    int64_t acc = 0;
    for (unsigned i = 0; i < SUMI_FIR_TAPS; ++i) {
        acc += (int32_t)sumi_fir[i] * s->history[at];
        at = at ? at - 1 : SUMI_FIR_TAPS - 1;
    }
    s->cursor = (s->cursor + 1) % SUMI_FIR_TAPS;
    int32_t filtered = (int32_t)(acc / 32768);
    s->phase += 16000;
    bool emit = s->phase >= 22050;
    if (emit) {
        s->phase -= 22050;
        int32_t value = filtered + (int64_t)(s->previous - filtered) * s->phase / 16000;
        *out = value > 32767 ? 32767 : value < -32768 ? -32768 : (int16_t)value;
    }
    s->previous = filtered;
    return emit;
}
