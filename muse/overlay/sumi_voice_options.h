#pragma once
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
bool sumi_native_voice_unavailable(void);
void sumi_native_voice_rejected(int http_status);
void sumi_native_voice_retry(void);
#ifdef __cplusplus
}
#endif
