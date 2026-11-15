#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
void sumi_asr_init(void);
bool sumi_asr_enabled(void);
bool sumi_asr_configured(void);
bool sumi_asr_select(bool enabled);
bool sumi_asr_set_key(const char *key);
bool sumi_asr_key_valid(const char *key);
/* Caller owns PCM and output. No automatic retry or transcript submission. */
bool sumi_asr_transcribe(const int16_t *pcm, size_t frames, char *out, size_t cap,
                        bool (*cancelled)(void), char *why, size_t why_cap);
#ifdef __cplusplus
}
#endif
