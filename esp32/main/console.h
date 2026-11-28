// Commands on the COM port for reading drives off the board (see console.c).
#pragma once

#include <stddef.h>

// dir: where drives are kept. df: total and used bytes there.
void console_start(const char *dir, void (*df)(size_t *total, size_t *used));
