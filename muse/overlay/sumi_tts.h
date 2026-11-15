#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
bool sumi_tts_start(const char *text);
/* Local, silent diagnostic; caller must ensure conversation is idle. */
bool sumi_tts_benchmark(void);
/* Relative to the previous firmware. Applied at the next utterance. */
int sumi_tts_rate(void);
void sumi_tts_set_rate(int percent);
void sumi_tts_cancel(void);
/* Non-blocking, at most 256 samples. done is set only after all queued PCM. */
size_t sumi_tts_read(int16_t pcm[256], bool *done, bool *failed);
#ifdef __cplusplus
}
#endif
