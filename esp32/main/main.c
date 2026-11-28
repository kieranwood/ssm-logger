// Stage 3: record every drive to flash, upload it over the home Wi-Fi.
//
// Waits for the ECU (ignition on), records every parameter it supports as raw SSM replies until the
// ignition goes off, and keeps the drive if the engine ran (drive_log.c). Drives are read off the
// board over the COM port with fetch_drives.py (console.c), or uploaded to log_server.py (uploader.c).
// (The stage 1 connection test is in git history: commit 39f2986.)
#include <stdio.h>

#include "config.h"
#include "console.h"
#include "drive_log.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "openport.h"
#include "uploader.h"

static const char *TAG = "main";

#define LOG_DIR "/logs"
#define LOG_PARTITION "logs"

static void df(size_t *total, size_t *used)
{
    *total = *used = 0;
    esp_littlefs_info(LOG_PARTITION, total, used);
}

static size_t free_bytes(void)
{
    size_t total, used;
    df(&total, &used);
    return total > used ? total - used : 0;
}

void app_main(void)
{
    printf("\nssm-esp32 stage 3: drive logger with Wi-Fi upload (read-only: nothing is written to the ECU)\n");
    ESP_ERROR_CHECK(config_init());
    const esp_vfs_littlefs_conf_t fs = {
        .base_path = LOG_DIR,
        .partition_label = LOG_PARTITION,
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_littlefs_register(&fs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Could not mount the log partition: %s", esp_err_to_name(err));
        return;
    }
    size_t total, used;
    df(&total, &used);
    ESP_LOGI(TAG, "Log storage: %u kB used of %u kB", (unsigned)(used / 1024), (unsigned)(total / 1024));

    console_start(LOG_DIR, df);
    if (op_usb_start() != ESP_OK) {
        ESP_LOGE(TAG, "USB host: %s", op_last_error());
        return;
    }
    dl_init(LOG_DIR, free_bytes);
    uploader_start(LOG_DIR);
    while (true) {
        if (dl_step()) uploader_kick();      // a drive just finished
        else vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
