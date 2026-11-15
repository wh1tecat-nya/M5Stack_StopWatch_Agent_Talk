#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <time.h>
#include <string.h>

/* Called from the UI task. Network synchronization never waits for a reply. */
void sumi_clock_poll(bool connected);
/* Format UTC as Japan time, independent of the process-wide timezone. */
static inline void sumi_clock_format(time_t utc, char out[6]) {
    if (utc < 1704067200) { /* Reject an unset clock (before 2024). */
        memcpy(out, "--:--", 6);
        return;
    }
    time_t jst = utc + 9 * 60 * 60;
    struct tm t;
    if (!gmtime_r(&jst, &t)) { memcpy(out, "--:--", 6); return; }
    out[0] = '0' + t.tm_hour / 10; out[1] = '0' + t.tm_hour % 10;
    out[2] = ':';
    out[3] = '0' + t.tm_min / 10; out[4] = '0' + t.tm_min % 10;
    out[5] = 0;
}
