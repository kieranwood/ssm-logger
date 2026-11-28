#pragma once
#include <stdint.h>
extern int64_t g_now_us;
static inline void vTaskDelay(uint32_t ms) { g_now_us += (int64_t)ms * 1000; }
static inline int xTaskCreatePinnedToCore(void (*f)(void *), const char *n, int s, void *a, int p, void *h, int c)
{ (void)f; (void)n; (void)s; (void)a; (void)p; (void)h; (void)c; return 1; }
