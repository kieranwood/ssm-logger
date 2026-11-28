#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
typedef struct { bool skip_phy_setup; int intr_flags; } usb_host_config_t;
typedef void *usb_device_handle_t;
typedef struct { uint16_t idVendor, idProduct; uint8_t bDeviceClass; } usb_device_desc_t;
#define USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS 1
static inline esp_err_t usb_host_install(const usb_host_config_t *c) { (void)c; return 0; }
static inline esp_err_t usb_host_lib_handle_events(uint32_t t, uint32_t *f) { (void)t; *f = 0; return 0; }
static inline esp_err_t usb_host_device_free_all(void) { return 0; }
esp_err_t usb_host_get_device_descriptor(usb_device_handle_t d, const usb_device_desc_t **out);
