// Commands on the COM port for reading drives off the board (see console.c).
#pragma once

#include <stddef.h>
#include <stdint.h>

// dir: where drives are kept. df: total and used bytes there.
void console_start(const char *dir, void (*df)(size_t *total, size_t *used));

// When someone last typed on the COM port (ms since boot; very negative if never). Keeps the board awake.
int64_t console_last_input_ms(void);
