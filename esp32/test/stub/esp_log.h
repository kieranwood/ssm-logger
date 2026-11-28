#pragma once
#include <stdio.h>
extern int g_quiet;
#define ESP_LOGI(t, f, ...) do { if (!g_quiet) printf("I %s: " f "\n", t, ##__VA_ARGS__); } while (0)
#define ESP_LOGW(t, f, ...) do { if (!g_quiet) printf("W %s: " f "\n", t, ##__VA_ARGS__); } while (0)
#define ESP_LOGE(t, f, ...) do { printf("E %s: " f "\n", t, ##__VA_ARGS__); } while (0)
