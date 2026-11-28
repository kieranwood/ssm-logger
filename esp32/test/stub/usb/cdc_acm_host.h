#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "esp_err.h"
#include "usb/usb_host.h"
#define CDC_HOST_ANY_VID 0
#define CDC_HOST_ANY_PID 0
#define CDC_HOST_ANY_DEV_ADDR 0
typedef struct cdc_dev_s *cdc_acm_dev_hdl_t;
typedef enum { CDC_ACM_HOST_ERROR, CDC_ACM_HOST_SERIAL_STATE, CDC_ACM_HOST_NETWORK_CONNECTION,
               CDC_ACM_HOST_DEVICE_DISCONNECTED } cdc_acm_host_dev_event_t;
typedef struct { cdc_acm_host_dev_event_t type; union { int error; cdc_acm_dev_hdl_t cdc_hdl; } data; }
    cdc_acm_host_dev_event_data_t;
typedef bool (*cdc_acm_data_callback_t)(const uint8_t *data, size_t data_len, void *user_arg);
typedef void (*cdc_acm_host_dev_callback_t)(const cdc_acm_host_dev_event_data_t *event, void *user_ctx);
typedef void (*cdc_acm_new_dev_callback_t)(usb_device_handle_t usb_dev);
typedef struct { size_t driver_task_stack_size; unsigned driver_task_priority; int xCoreID;
                 cdc_acm_new_dev_callback_t new_dev_cb; } cdc_acm_host_driver_config_t;
typedef struct { uint16_t vid, pid; uint8_t interface_idx, dev_addr; uint32_t connection_timeout_ms;
                 size_t out_buffer_size, in_buffer_size; cdc_acm_host_dev_callback_t event_cb;
                 cdc_acm_data_callback_t data_cb; void *user_arg; } cdc_acm_host_open_config_t;
typedef struct { uint32_t dwDTERate; uint8_t bCharFormat, bParityType, bDataBits; } cdc_acm_line_coding_t;
esp_err_t cdc_acm_host_install(const cdc_acm_host_driver_config_t *c);
esp_err_t cdc_acm_host_open(const cdc_acm_host_open_config_t *c, cdc_acm_dev_hdl_t *h);
esp_err_t cdc_acm_host_close(cdc_acm_dev_hdl_t h);
esp_err_t cdc_acm_host_data_tx_blocking(cdc_acm_dev_hdl_t h, const uint8_t *d, size_t n, uint32_t t);
esp_err_t cdc_acm_host_line_coding_set(cdc_acm_dev_hdl_t h, const cdc_acm_line_coding_t *c);
esp_err_t cdc_acm_host_set_control_line_state(cdc_acm_dev_hdl_t h, bool dtr, bool rts);
void cdc_acm_host_desc_print(cdc_acm_dev_hdl_t h);
