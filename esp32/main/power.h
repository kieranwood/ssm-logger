// Deep sleep while parked (for a board on constant 12 V power), waking when the engine runs.
//
// The car battery is measured through a resistor divider into GPIO4 (see ../README.md, "Sleep").
// Asleep, the board wakes every 5 s for a moment to read it. It boots and logs when the battery is
// above the wake voltage (13.2 V: the alternator is charging, so the engine is running) and at least
// 0.4 V above the lowest reading since it went to sleep, on two checks in a row. A battery settling
// after the engine stops only falls, so it never wakes the board; restarting the engine, even a few
// minutes later with the battery still above 13.2 V, jumps it to about 14 V.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

// First thing in app_main: on a timer wake with the engine off, goes straight back to sleep.
void power_early(void);

// After config_init: load the settings.
void power_init(void);

// Car battery voltage through the divider, in mV (about 0 if there's no divider).
int power_battery_mv(void);

// The OpenPort's reading of the same battery (when the ECU connects): calibrates the divider.
void power_calibrate(int obd_mv);

bool power_sleep_enabled(void);

// Turn sleep on or off. On is refused (with the reason in why) without a believable divider reading.
esp_err_t power_set_sleep(bool on, char *why, size_t cap);

// Wake above this many mV (default 13200).
esp_err_t power_set_wake_mv(int mv);

// The battery is low enough that the board should sleep (when the ECU isn't answering).
bool power_battery_low(void);

// Go to deep sleep now. Never returns.
void power_sleep(const char *why);

// Battery, settings and calibration, in words (for the console).
const char *power_status(void);
