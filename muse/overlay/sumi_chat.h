#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define SUMI_CHAT_LEGACY_ID "3622d9af-4010-4247-ac57-cd702e8bc9d3"
#define SUMI_CHAT_ID_BYTES 36
#define SUMI_CHAT_TITLE_BYTES 64
#define SUMI_CHAT_MAX 64
#define SUMI_CHAT_CATALOG_MAX (2 + SUMI_CHAT_MAX * (SUMI_CHAT_ID_BYTES + 1 + SUMI_CHAT_TITLE_BYTES))

void sumi_chat_init(int legacy);
bool sumi_chat_sync(const uint8_t *bytes, size_t size);
uint32_t sumi_chat_catalog_hash(void);
int sumi_chat_count(void);
int sumi_chat_selected(void);
bool sumi_chat_select(int index);
void sumi_chat_id(char out[SUMI_CHAT_ID_BYTES + 1]);
void sumi_chat_title(int index, char out[SUMI_CHAT_TITLE_BYTES + 1]);
bool sumi_chat_matches(const char *expected, const char *actual);
bool sumi_chat_wearable(void);
void sumi_chat_set_wearable(bool enabled);
// Takes ownership of malloc-allocated text, including on failure.
bool sumi_chat_transcript(char *text);
#ifdef __cplusplus
}
#endif
