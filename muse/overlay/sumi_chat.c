#include "sumi_chat.h"
#include <stdatomic.h>
#include <string.h>

static atomic_int selected = 1;
static atomic_bool wearable = true;
bool sumi_chat_wearable(void) { return atomic_load(&wearable); }
void sumi_chat_set_wearable(bool enabled) { atomic_store(&wearable, enabled); }
static const char *const sessions[] = {"", "3622d9af-4010-4247-ac57-cd702e8bc9d3"};
int sumi_chat_selected(void) { return atomic_load(&selected); }
void sumi_chat_select(int index) { if (index >= 0 && index < 2) atomic_store(&selected, index); }
const char *sumi_chat_id(void) { return sessions[sumi_chat_selected()]; }
bool sumi_chat_matches(const char *expected, const char *actual) {
    return !expected[0] || (actual && !strcmp(expected, actual));
}
