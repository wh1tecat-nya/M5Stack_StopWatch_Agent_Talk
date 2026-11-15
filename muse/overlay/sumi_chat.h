#pragma once
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Registered existing chats. Selection is persisted by the settings UI. */
int sumi_chat_selected(void);
void sumi_chat_select(int index);
const char *sumi_chat_id(void);
bool sumi_chat_matches(const char *expected, const char *actual);
/* Whether outgoing chat requests include device: mcu-wearable. */
bool sumi_chat_wearable(void);
void sumi_chat_set_wearable(bool enabled);
/* Takes ownership of malloc-allocated text, including on failure. */
bool sumi_chat_transcript(char *text);
#ifdef __cplusplus
}
#endif
