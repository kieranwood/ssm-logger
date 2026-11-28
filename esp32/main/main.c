// Stage 1: connection test. Plug the OpenPort into the board's "USB" port, watch the "COM" port.
//
// Over and over, every few seconds:
//   1. wait for a USB device and open it as a serial port (prints its VID:PID and descriptors once)
//   2. identify the OpenPort (ati) and read the OBD pin 16 voltage (atr 16)
//   3. open the ISO15765 channel and send the SSM init (0xAA); with the ignition on the ECU answers
//      with its ID
//   4. if it did: read engine speed, coolant and battery voltage 100 times (the same read-only
//      0xA8 request the logger makes) and report the samples per second
// and print a one-line RESULT you can paste back.
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "openport.h"

static const char *TAG = "test";

#define SSM_ECU_INIT 0xAA
#define SSM_READ_ADDRESSES 0xA8
#define SSM_READ_REPLY 0xE8

static void run_once(bool *descriptors_shown)
{
    ESP_LOGI(TAG, "Waiting for the OpenPort on the USB port...");
    if (op_open(5000) != ESP_OK) {
        printf("RESULT: FAIL usb - %s\n", op_last_error());
        return;
    }
    bool expected = op_vid() == OPENPORT_VID && (op_pid() == OPENPORT_PID || op_pid() == OPENPORT_PID_ALT);
    ESP_LOGI(TAG, "Opened USB device %04x:%04x%s", op_vid(), op_pid(),
             expected ? " (OpenPort 2.0)" : " - not an OpenPort ID (0403:cc4c or cc4d)");
    if (!*descriptors_shown) {
        op_print_descriptors();
        *descriptors_shown = true;
    }
    ESP_LOGI(TAG, "OpenPort says: %s", op_firmware());

    int mv = 0;
    if (op_battery_mv(&mv) != ESP_OK) {
        printf("RESULT: FAIL voltage - %s\n", op_last_error());
        op_close();
        return;
    }
    ESP_LOGI(TAG, "OBD pin 16 (car battery): %.2f V", mv / 1000.0);

    if (op_connect_can() != ESP_OK) {
        printf("RESULT: FAIL can - %s (OpenPort OK, %.2f V)\n", op_last_error(), mv / 1000.0);
        op_close();
        return;
    }
    uint8_t reply[300];
    const uint8_t init = SSM_ECU_INIT;
    int n = op_request(&init, 1, reply, sizeof reply, 2000);
    if (n < 9 || reply[0] != 0xEA) {
        printf("RESULT: PARTIAL OpenPort OK (%s, %.2f V), no ECU reply - is the ignition on? (%s)\n",
               op_firmware(), mv / 1000.0, n < 0 ? op_last_error() : "unexpected reply");
        op_close();
        return;
    }
    // init = reply[1:]; ECU ID = init[3:8]
    char ecu[11];
    for (int i = 0; i < 5; i++) sprintf(ecu + 2 * i, "%02X", reply[4 + i]);
    ESP_LOGI(TAG, "ECU %s answered (%d bytes of capabilities)", ecu, n - 1);

    // Engine speed (0x0E-0x0F, /4 rpm), coolant (0x08, -40 C), battery (0x1C, *8/100 V)
    const uint8_t read[] = {SSM_READ_ADDRESSES, 0x00, 0, 0, 0x0E, 0, 0, 0x0F, 0, 0, 0x08, 0, 0, 0x1C};
    int ok = 0;
    int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < 100; i++) {
        n = op_request(read, sizeof read, reply, sizeof reply, 500);
        if (n != 5 || reply[0] != SSM_READ_REPLY) continue;
        ok++;
        if (i % 20 == 0) {
            ESP_LOGI(TAG, "engine %.0f rpm, coolant %d C, battery %.2f V",
                     (reply[1] << 8 | reply[2]) / 4.0, reply[3] - 40, reply[4] * 8 / 100.0);
        }
    }
    double secs = (esp_timer_get_time() - t0) / 1e6;
    printf("RESULT: PASS ECU %s, %s, %.2f V, %d/100 reads, %.1f reads/s, %lu bytes dropped, %lu resyncs\n",
           ecu, op_firmware(), mv / 1000.0, ok, ok / secs, (unsigned long)op_rx_overflows(),
           (unsigned long)op_resyncs());
    op_close();
}

void app_main(void)
{
    printf("\nssm-esp32 stage 1: OpenPort connection test\n"
           "Plug the OpenPort into the board's USB port (not COM). Read-only: nothing is written to the ECU.\n\n");
    if (op_usb_start() != ESP_OK) {
        printf("RESULT: FAIL setup - %s\n", op_last_error());
        return;
    }
    bool descriptors_shown = false;
    while (true) {
        run_once(&descriptors_shown);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
