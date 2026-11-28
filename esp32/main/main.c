// Stage 4: record every drive to flash, upload it over the home Wi-Fi, sleep while parked.
//
// Waits for the ECU (ignition on), records every parameter it supports as raw SSM replies until the
// ignition goes off, and keeps the drive if the engine ran (drive_log.c). Drives are read off the
// board over the COM port with fetch_drives.py (console.c), or uploaded to log_server.py (uploader.c).
// On constant power with sleep turned on, it deep-sleeps once a drive is over and uploaded, and wakes
// when the engine runs (power.c).
// (The stage 1 connection test is in git history: commit 39f2986.)
#include <stdio.h>

#include "config.h"
#include "console.h"
#include "drive_log.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "openport.h"
#include "power.h"
#include "uploader.h"

static const char *TAG = "main";

#define LOG_DIR "/logs"
#define LOG_PARTITION "logs"
#define SLEEP_AFTER_ECU_MS 60000        // ECU silent this long (and uploads tried): sleep
#define SLEEP_AFTER_CONSOLE_MS 300000   // ...but not within 5 minutes of someone using the COM port
#define LOW_BATTERY_AFTER_MS 30000      // battery low and the ECU silent this long: sleep now

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

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

void app_main(void)
{
    power_early();                       // a wake check with the engine off goes back to sleep here
    printf("\nssm-esp32 stage 4: drive logger with Wi-Fi upload and sleep (read-only: nothing is written to "
           "the ECU)\n");
    ESP_ERROR_CHECK(config_init());
    power_init();
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
    dl_on_connect(power_calibrate);
    uploader_start(LOG_DIR);
    ESP_LOGI(TAG, "Power: %s", power_status());
    int64_t ecu_last_seen = now_ms();
    unsigned rounds_after_drive = uploader_rounds();
    while (true) {
        if (dl_step()) {                     // a drive just finished
            uploader_kick();
            ecu_last_seen = now_ms();
            rounds_after_drive = uploader_rounds();
        } else {
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
        if (!power_sleep_enabled()) continue;
        int64_t quiet = now_ms() - ecu_last_seen;
        if (power_battery_low() && quiet > LOW_BATTERY_AFTER_MS) {
            power_sleep("battery low");
        }
        bool uploads_tried = uploader_rounds() != rounds_after_drive && !uploader_busy();
        if (quiet > SLEEP_AFTER_ECU_MS && uploads_tried && now_ms() - console_last_input_ms() > SLEEP_AFTER_CONSOLE_MS) {
            power_sleep("parked, uploads tried");
        }
    }
}
