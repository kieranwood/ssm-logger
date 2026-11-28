// Settings kept in the board's NVS (set over the COM port: `set ssid …`), so no Wi-Fi password or
// token is ever compiled into the firmware.
#pragma once

#include <stddef.h>

#include "esp_err.h"

typedef struct {
    char ssid[33];       // home Wi-Fi
    char pass[65];
    char url[128];       // log_server.py, e.g. http://192.168.1.20:8765
    char token[96];      // the token log_server.py was started with
} logger_config_t;

// Initialise NVS (also needed by Wi-Fi). Call once, first.
esp_err_t config_init(void);

void config_load(logger_config_t *c);

// key: ssid, pass, url or token (or power.c's sleep, wake_mv, adc_scale). An empty value clears it.
esp_err_t config_set(const char *key, const char *value);

// Read one setting as a string (empty if unset).
esp_err_t config_get(const char *key, char *out, size_t cap);
