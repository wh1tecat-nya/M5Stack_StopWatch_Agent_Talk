#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* Host-testable rules shared by the UI and speech worker. */
typedef enum { SUMI_AVATAR, SUMI_BADGE, SUMI_DIM, SUMI_OFF } sumi_screen_t;
static inline sumi_screen_t sumi_next_screen(sumi_screen_t screen, bool busy,
                                             uint32_t elapsed, const int seconds[3]) {
    if (busy) return SUMI_AVATAR;
    return screen < SUMI_OFF && elapsed >= (uint32_t)seconds[screen] * 1000
        ? (sumi_screen_t)(screen + 1) : screen;
}
static inline size_t sumi_utf8_chunk(const char *text, size_t limit) {
    size_t n = strlen(text);
    if (n <= limit) return n;
    n = limit;
    while (n && ((unsigned char)text[n] & 0xc0) == 0x80) --n;
    size_t boundary = 0;
    for (size_t i = 0; i < n; ++i) {
        if (text[i] == '\n' || text[i] == '.' || text[i] == '!' || text[i] == ' ')
            boundary = i + 1;
        if (i + 3 <= n && (!memcmp(text + i, "。", 3) || !memcmp(text + i, "、", 3)))
            boundary = i + 3;
    }
    return boundary >= n / 2 ? boundary : n;
}
#include "sumi_resampler.h"
