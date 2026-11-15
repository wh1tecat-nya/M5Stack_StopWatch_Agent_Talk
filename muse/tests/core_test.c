#include "sumi_core.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

static void screens(void) {
    int seconds[3] = {30, 60, 60};
    assert(sumi_next_screen(SUMI_AVATAR, false, 29999, seconds) == SUMI_AVATAR);
    assert(sumi_next_screen(SUMI_AVATAR, false, 30000, seconds) == SUMI_BADGE);
    assert(sumi_next_screen(SUMI_BADGE, false, 60000, seconds) == SUMI_DIM);
    assert(sumi_next_screen(SUMI_DIM, false, 60000, seconds) == SUMI_OFF);
    for (int i = 0; i < 4; ++i)
        assert(sumi_next_screen((sumi_screen_t)i, true, 999999, seconds) == SUMI_AVATAR);
    assert(sumi_next_screen(SUMI_OFF, false, UINT32_MAX, seconds) == SUMI_OFF);
}
static void chunks(void) {
    const char *s = "今日は良い天気です。This is a longer response. 絵文字🐈も境界で切れません。次の文章です。";
    for (size_t limit = 4; limit < 100; ++limit) {
        size_t offset = 0;
        while (s[offset]) {
            size_t n = sumi_utf8_chunk(s + offset, limit);
            assert(n > 0 && n <= limit);
            offset += n;
            assert(!s[offset] || ((unsigned char)s[offset] & 0xc0) != 0x80);
        }
        assert(offset == strlen(s));
    }
}
static void resampling(void) {
    sumi_resampler_t s = {0}; size_t emitted = 0;
    for (size_t i = 0; i < 22050 * 3; ++i) {
        int16_t out;
        if (sumi_resample_one(&s, 1234, &out)) {
            assert(out == 1234); ++emitted;
        }
    }
    assert(emitted == 16000 * 3);
    /* A full-scale alternating signal stays within range, including across
       arbitrarily-sized input callbacks (the state persists). */
    s = (sumi_resampler_t){0};
    for (size_t i = 0; i < 100000; ++i) {
        int16_t out;
        sumi_resample_one(&s, i & 1 ? 32767 : -32768, &out);
    }
}
static double tone_rms(double hz) {
    sumi_resampler_t s = {0}; double square = 0; size_t count = 0;
    for (size_t i=0;i<22050;++i) {
        int16_t output;
        if (sumi_resample_one(&s,(int16_t)lround(12000*sin(6.283185307179586*hz*i/22050)),&output) && i>200) {
            square += (double)output*output; ++count;
        }
    }
    return sqrt(square/count);
}
static void alias_rejection(void) {
    double speech=tone_rms(1000);
    assert(speech>8200 && speech<8600); /* preserve the voice band */
    for(double hz=8000;hz<=10800;hz+=400) assert(tone_rms(hz)<speech*0.0032); /* >49 dB rejection */
    printf("1 kHz RMS %.1f; 9 kHz folded energy RMS %.3f\n",speech,tone_rms(9000));
}
int main(void) { screens(); chunks(); resampling(); alias_rejection(); puts("screen phases, UTF-8 chunks, anti-aliased resampler: PASS"); }
