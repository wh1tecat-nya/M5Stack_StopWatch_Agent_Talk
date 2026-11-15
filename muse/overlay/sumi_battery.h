#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Voltage-derived display estimate, not a coulomb counter. Raw mV stays intact
 * in muse_power_t for diagnostics and battery measurements. */
typedef struct {
    int samples[5], count, cursor, percent, direction;
    double filtered_mv;
    bool initialized, usb;
    int64_t last_ms, stable_after, changed_ms, direction_ms, low_since;
} sumi_battery_t;

static inline int sumi_battery_percent(int mv) {
    int pct = (int)((-(int64_t)mv * mv + 9016LL * mv - 19189000) / 10000);
    return pct < 0 ? 0 : pct > 100 ? 100 : pct;
}

static inline int sumi_battery_update(sumi_battery_t *s, int mv, bool usb, int64_t now) {
    /* Do not turn corrupt ADC / failed I2C reads into an empty battery. */
    if (mv < 2500 || mv > 4500) return s->initialized ? s->percent : -1;
    if (!s->initialized) {
        *s = (sumi_battery_t){.initialized = true, .usb = usb,
            .filtered_mv = mv, .percent = sumi_battery_percent(mv),
            .last_ms = now, .changed_ms = now, .low_since = -1};
    }
    if (usb != s->usb) {
        s->usb = usb; s->count = s->cursor = 0;
        s->stable_after = now + 20000; s->direction = 0;
        s->low_since = -1;
    }
    s->samples[s->cursor] = mv;
    s->cursor = (s->cursor + 1) % 5;
    if (s->count < 5) ++s->count;
    int sorted[5];
    for (int i = 0; i < s->count; ++i) {
        sorted[i] = s->samples[i];
        for (int j = i; j && sorted[j] < sorted[j - 1]; --j) {
            int t = sorted[j]; sorted[j] = sorted[j - 1]; sorted[j - 1] = t;
        }
    }
    int median = sorted[s->count / 2];
    int64_t dt = now - s->last_ms;
    s->last_ms = now;
    if (dt < 0) dt = 0;
    if (dt > 10000) dt = 10000; /* A long pause is not a series of samples. */
    s->filtered_mv += (median - s->filtered_mv) * (double)dt / (45000 + dt);

    /* A persistently depleted cell must not be hidden by smoothing. Require
     * a complete median window and 10 seconds; isolated dips cannot trigger. */
    if (s->count == 5 && median <= 3400) {
        if (s->low_since < 0) s->low_since = now;
        if (now - s->low_since >= 10000) {
            int low = sumi_battery_percent(median);
            if (s->percent > low) s->percent = low;
            s->changed_ms = now;
        }
    } else s->low_since = -1;

    if (now < s->stable_after || s->count < 5) return s->percent;
    int target = sumi_battery_percent((int)(s->filtered_mv + 0.5));
    int delta = target - s->percent;
    /* Two points of hysteresis; endpoints remain reachable. Recovery on
     * battery is allowed only after 2 minutes of sustained higher voltage. */
    int dir = delta >= 2 || (target == 100 && delta > 0) ? 1 :
        delta <= -2 || (target == 0 && delta < 0) ? -1 : 0;
    if (dir != s->direction) { s->direction = dir; s->direction_ms = now; }
    int64_t delay = dir > 0 && !usb ? 120000 : 10000;
    int64_t interval = dir > 0 && !usb ? 60000 : 20000;
    if (dir && now - s->direction_ms >= delay && now - s->changed_ms >= interval) {
        s->percent += dir;
        s->changed_ms = now;
    }
    return s->percent;
}
