// Uploading finished drives to log_server.py over the home Wi-Fi (see uploader.c).
#pragma once

#include <stdbool.h>

// dir: where drives are kept. Starts Wi-Fi (off until needed) and the upload task.
void uploader_start(const char *dir);

// Try now instead of at the next 30-second check (after a drive, or the `upload` command).
void uploader_kick(void);

// What it last did, in words (for the console).
const char *uploader_status(void);
