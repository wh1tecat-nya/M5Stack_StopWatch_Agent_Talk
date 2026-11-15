#pragma once
#include <stdbool.h>
/* M5PM1 GPIO3: 185 mA = input without pull, 425 mA = push-pull LOW. */
bool sumi_charge_set_current(int milliamps);
/* Last read-back-verified mode, or zero if the hardware state is unknown. */
int sumi_charge_current(void);
/* Charging only; USB supply and other power rails are preserved. */
bool sumi_charge_enable(bool enabled);
/* 1 = enabled, 0 = disabled, -1 = I2C read failed. */
int sumi_charge_enabled(void);
/* External Grove 5 V boost enable bit: 1=on, 0=off, -1=read failed. */
int sumi_ext_output_enabled(void);
