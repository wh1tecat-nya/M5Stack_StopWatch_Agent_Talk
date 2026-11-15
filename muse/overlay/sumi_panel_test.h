#pragma once
#include <stdbool.h>
void sumi_panel_test_request(void);
bool sumi_panel_test_active(void);
int sumi_panel_test_state(void); /* 0 idle, 1 queued, 2 paused, 3 restoring, 4 done */
