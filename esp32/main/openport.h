// Minimal Tactrix OpenPort 2.0 driver over USB host (CDC-ACM), for the ESP32-S3.
// A C port of the parts of ../../openport.py that SSM logging needs: the same text commands
// (ati, ata, atr 16, ato6, ats6, atf6, att6, atc6, atz) and the same binary packet parsing.
// One cable at a time; call everything from one task.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define OPENPORT_VID 0x0403
#define OPENPORT_PID 0xCC4C
#define OPENPORT_PID_ALT 0xCC4D        // seen on firmware 1.17.4877

// Install the USB host stack and the CDC-ACM driver. Call once.
esp_err_t op_usb_start(void);

// Wait up to timeout_ms for a USB device on the S3's USB port, open it and identify it (ati, ata).
esp_err_t op_open(uint32_t timeout_ms);

// After op_open: what was plugged in, and the OpenPort's "ari ..." identification line.
uint16_t op_vid(void);
uint16_t op_pid(void);
const char *op_firmware(void);

// Print the USB descriptors of the open device (for diagnosing an unexpected device).
void op_print_descriptors(void);

// Voltage on OBD pin 16 (the car battery), in millivolts.
esp_err_t op_battery_mv(int *mv);

// Open an ISO15765 channel to the ECU (500 kbit/s, tester 0x7E0, ECU 0x7E8), as RomRaider does.
esp_err_t op_connect_can(void);

// Send a request to the ECU and wait for its reply (without the CAN id).
// Returns the reply length, or -1 if nothing came back within timeout_ms (or on error).
int op_request(const uint8_t *payload, size_t len, uint8_t *reply, size_t cap, uint32_t timeout_ms);

// Close the channel and the device. Safe to call at any time.
void op_close(void);

// True while a device is open and still plugged in.
bool op_is_open(void);

// Why the last call failed, in words.
const char *op_last_error(void);

// Bytes dropped because the receive buffer was full, and packet resyncs (lost bytes), since op_open.
uint32_t op_rx_overflows(void);
uint32_t op_resyncs(void);
