#include "sumi_voice_options.h"
#include <stdatomic.h>
static atomic_bool unavailable;
bool sumi_native_voice_unavailable(void) { return atomic_load(&unavailable); }
void sumi_native_voice_rejected(int status) { if (status == 404) atomic_store(&unavailable, true); }
void sumi_native_voice_retry(void) { atomic_store(&unavailable, false); }
