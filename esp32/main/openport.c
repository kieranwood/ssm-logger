// See openport.h. The parsing mirrors OpenPort._next_item / _handle_packet in ../../openport.py.
#include "openport.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "usb/cdc_acm_host.h"
#include "usb/usb_host.h"

static const char *TAG = "openport";

#define ISO15765 6
#define CH_ISO15765 ('0' + ISO15765)
#define TX_ISO15765_FRAME_PAD 0x40
#define FLOW_CONTROL_FILTER 3

// J2534 SET_CONFIG parameter ids (as in openport.py)
#define CFG_LOOPBACK 0x03
#define CFG_ISO15765_BS 0x1E
#define CFG_ISO15765_STMIN 0x1F
#define CFG_BS_TX 0x22
#define CFG_STMIN_TX 0x23
#define CFG_ISO15765_WFT_MAX 0x25

// Packet types
#define NORM_MSG 0x00
#define RX_MSG_END_IND 0x40
#define EXT_ADDR_MSG_END_IND 0x44

#define TESTER_ID 0x7E0
#define ECU_ID 0x7E8

#define RX_STREAM_SIZE 8192        // bytes from the USB callback waiting to be parsed
#define PARSE_BUF_SIZE 1024        // bytes being parsed (a packet is at most 259)
#define MSG_MAX 512                // one reassembled ISO15765 message
#define MSG_QUEUE 4

static StreamBufferHandle_t s_rx;
static cdc_acm_dev_hdl_t s_dev;
static volatile bool s_gone;       // the device was unplugged
static volatile uint32_t s_overflows;
static uint32_t s_resyncs;
static uint16_t s_vid, s_pid;
static volatile uint16_t s_new_vid, s_new_pid;
static bool s_channel_open;
static char s_firmware[96];
static char s_error[160];

static uint8_t s_buf[PARSE_BUF_SIZE];
static size_t s_len;

static uint8_t s_parts[MSG_MAX];
static size_t s_parts_len;
static uint8_t s_queue[MSG_QUEUE][MSG_MAX];
static size_t s_queue_len[MSG_QUEUE];
static int s_queue_n;

static const char *J2534_ERRORS[] = {
    "", "not supported", "invalid channel id", "invalid protocol id", "null parameter",
    "invalid ioctl value", "invalid flags", "failed", "device not connected",
    "timeout (nothing on the CAN bus acknowledged the message - is the car connected with ignition on?)",
    "invalid message", "invalid time interval", "exceeded limit", "invalid message id",
    "device in use", "invalid ioctl id", "buffer empty", "buffer full", "buffer overflow",
    "pin invalid", "channel in use", "message protocol id", "invalid filter id",
    "no flow control", "not unique", "invalid baud rate", "invalid device id",
};

static esp_err_t fail(esp_err_t err, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_error, sizeof s_error, fmt, ap);
    va_end(ap);
    return err;
}

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

// ------------------------------------------------------------------ USB

static void usb_lib_task(void *arg)
{
    while (true) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            usb_host_device_free_all();
        }
    }
}

static void on_new_device(usb_device_handle_t usb_dev)
{
    const usb_device_desc_t *desc;
    if (usb_host_get_device_descriptor(usb_dev, &desc) == ESP_OK) {
        s_new_vid = desc->idVendor;
        s_new_pid = desc->idProduct;
        ESP_LOGI(TAG, "USB device attached: %04x:%04x, class 0x%02x", desc->idVendor, desc->idProduct,
                 desc->bDeviceClass);
    }
}

static bool on_rx(const uint8_t *data, size_t len, void *arg)
{
    size_t sent = xStreamBufferSend(s_rx, data, len, 0);
    if (sent < len) {
        s_overflows += len - sent;
    }
    return true;
}

static void on_event(const cdc_acm_host_dev_event_data_t *event, void *arg)
{
    switch (event->type) {
    case CDC_ACM_HOST_DEVICE_DISCONNECTED:
        ESP_LOGW(TAG, "OpenPort unplugged");
        s_gone = true;
        break;
    case CDC_ACM_HOST_ERROR:
        ESP_LOGE(TAG, "CDC-ACM error %d", event->data.error);
        break;
    default:
        break;
    }
}

esp_err_t op_usb_start(void)
{
    s_rx = xStreamBufferCreate(RX_STREAM_SIZE, 1);
    if (!s_rx) return fail(ESP_ERR_NO_MEM, "no memory for the receive buffer");
    const usb_host_config_t host_config = {.skip_phy_setup = false, .intr_flags = ESP_INTR_FLAG_LEVEL1};
    esp_err_t err = usb_host_install(&host_config);
    if (err != ESP_OK) return fail(err, "USB host install failed: %s", esp_err_to_name(err));
    xTaskCreatePinnedToCore(usb_lib_task, "usb_lib", 4096, NULL, 10, NULL, 0);
    const cdc_acm_host_driver_config_t driver_config = {
        .driver_task_stack_size = 4096,
        .driver_task_priority = 10,
        .xCoreID = 0,
        .new_dev_cb = on_new_device,
    };
    err = cdc_acm_host_install(&driver_config);
    if (err != ESP_OK) return fail(err, "CDC-ACM driver install failed: %s", esp_err_to_name(err));
    return ESP_OK;
}

static esp_err_t write_all(const uint8_t *data, size_t len)
{
    if (!s_dev || s_gone) return fail(ESP_ERR_INVALID_STATE, "OpenPort not connected");
    esp_err_t err = cdc_acm_host_data_tx_blocking(s_dev, data, len, 1000);
    if (err != ESP_OK) return fail(err, "USB write failed: %s", esp_err_to_name(err));
    return ESP_OK;
}

// Move whatever the USB callback has received into the parse buffer, waiting up to wait_ms.
static bool fill(uint32_t wait_ms)
{
    if (s_len >= sizeof s_buf) {               // can't happen with a sane device; never wedge
        s_len = 0;
    }
    size_t got = xStreamBufferReceive(s_rx, s_buf + s_len, sizeof s_buf - s_len, pdMS_TO_TICKS(wait_ms));
    s_len += got;
    return got > 0;
}

static void drop(size_t n)
{
    memmove(s_buf, s_buf + n, s_len - n);
    s_len -= n;
}

// ------------------------------------------------------------------ parsing

typedef enum { ITEM_NONE, ITEM_PKT, ITEM_LINE } item_t;

static uint8_t s_pkt_type, s_pkt_ch;
static uint8_t s_pkt_data[260];
static size_t s_pkt_len;
static char s_line[128];

static bool is_channel(uint8_t c) { return c == CH_ISO15765 || c == '5' || c == '4' || c == '3'; }

// Pop one complete item from the parse buffer (as OpenPort._next_item).
static item_t next_item(void)
{
    while (true) {
        size_t i = 0;
        while (i + 1 < s_len && !(s_buf[i] == 'a' && s_buf[i + 1] == 'r')) i++;
        if (i + 1 >= s_len) {                  // no "ar" yet: keep a possible trailing 'a'
            if (s_len > 1) drop(s_len - 1);
            return ITEM_NONE;
        }
        if (i) drop(i);
        if (s_len < 3) return ITEM_NONE;
        uint8_t ch = s_buf[2];
        if (is_channel(ch)) {                  // binary packet on a channel
            if (s_len < 4) return ITEM_NONE;
            size_t total = (size_t)s_buf[3] + 4;
            if (s_len < total) return ITEM_NONE;
            // A packet header inside this one means bytes were lost and the length is wrong:
            // drop the broken packet and pick up at that header.
            bool resynced = false;
            for (size_t k = 4; k + 3 < s_len && k < total; k++) {
                if (s_buf[k] == 'a' && s_buf[k + 1] == 'r' && (s_buf[k + 2] == '5' || s_buf[k + 2] == CH_ISO15765) &&
                    s_buf[k + 3] <= 0x20) {
                    drop(k);
                    s_resyncs++;
                    resynced = true;
                    break;
                }
            }
            if (resynced) continue;
            s_pkt_ch = ch;
            s_pkt_type = s_buf[4];
            s_pkt_len = total > 9 ? total - 9 : 0;
            memcpy(s_pkt_data, s_buf + 9, s_pkt_len);
            drop(total);
            return ITEM_PKT;
        }
        for (size_t e = 0; e + 1 < s_len; e++) {
            if (s_buf[e] == '\r' && s_buf[e + 1] == '\n') {
                size_t n = e < sizeof s_line - 1 ? e : sizeof s_line - 1;
                memcpy(s_line, s_buf, n);
                s_line[n] = 0;
                drop(e + 2);
                return ITEM_LINE;
            }
        }
        return ITEM_NONE;
    }
}

// Reassemble ISO15765 messages (as OpenPort._handle_packet).
static void handle_packet(void)
{
    if (s_pkt_ch != CH_ISO15765) return;
    if (s_pkt_type != NORM_MSG && s_pkt_type != RX_MSG_END_IND && s_pkt_type != EXT_ADDR_MSG_END_IND) return;
    const uint8_t *data = s_pkt_data;
    size_t len = s_pkt_len;
    // Long messages arrive in several packets, each repeating the 4-byte CAN id.
    if (s_parts_len >= 4 && len >= 4 && memcmp(data, s_parts, 4) == 0) {
        data += 4;
        len -= 4;
    }
    if (s_parts_len + len <= MSG_MAX) {
        memcpy(s_parts + s_parts_len, data, len);
        s_parts_len += len;
    }
    if (s_pkt_type != NORM_MSG) {
        if (s_parts_len && s_queue_n < MSG_QUEUE) {
            memcpy(s_queue[s_queue_n], s_parts, s_parts_len);
            s_queue_len[s_queue_n++] = s_parts_len;
        }
        s_parts_len = 0;
    }
}

static esp_err_t device_error(const char *line, const char *context)
{
    int code = atoi(line + 3);
    const char *what = code > 0 && code < (int)(sizeof J2534_ERRORS / sizeof *J2534_ERRORS) ? J2534_ERRORS[code] : "";
    return fail(ESP_FAIL, "OpenPort error %d %s%s%s", code, what, context ? " after " : "", context ? context : "");
}

// Send a text command (plus binary payload) and wait for a reply line starting with expect.
static esp_err_t command(const char *text, const char *expect, uint32_t timeout_ms,
                         const uint8_t *payload, size_t payload_len, char *reply, size_t reply_cap)
{
    uint8_t out[160];
    size_t n = strlen(text);
    if (n + payload_len > sizeof out) return fail(ESP_ERR_INVALID_SIZE, "command too long");
    memcpy(out, text, n);
    if (payload_len) memcpy(out + n, payload, payload_len);
    esp_err_t err = write_all(out, n + payload_len);
    if (err != ESP_OK) return err;
    char cmd[24];                               // the command's first line, for error messages
    const char *first = text + strspn(text, "\r\n");
    snprintf(cmd, sizeof cmd, "%.*s", (int)strcspn(first, "\r\n"), first);
    int64_t deadline = now_ms() + timeout_ms;
    while (now_ms() < deadline) {
        if (s_gone) return fail(ESP_ERR_INVALID_STATE, "OpenPort unplugged");
        item_t item = next_item();
        if (item == ITEM_NONE) {
            fill(20);
        } else if (item == ITEM_PKT) {
            handle_packet();
        } else if (strncmp(s_line, "are", 3) == 0) {
            return device_error(s_line, cmd);
        } else if (strncmp(s_line, expect, strlen(expect)) == 0) {
            if (reply) snprintf(reply, reply_cap, "%s", s_line);
            return ESP_OK;
        }
    }
    return fail(ESP_ERR_TIMEOUT, "no '%s' reply to %s", expect, cmd);
}

// ------------------------------------------------------------------ device

esp_err_t op_open(uint32_t timeout_ms)
{
    op_close();
    s_gone = false;
    s_overflows = 0;
    s_resyncs = 0;
    s_len = 0;
    s_parts_len = 0;
    s_queue_n = 0;
    xStreamBufferReset(s_rx);
    const cdc_acm_host_open_config_t cfg = {
        .vid = CDC_HOST_ANY_VID,              // anything: an unexpected device is reported, not ignored
        .pid = CDC_HOST_ANY_PID,
        .interface_idx = 0,
        .dev_addr = CDC_HOST_ANY_DEV_ADDR,
        .connection_timeout_ms = timeout_ms,
        .out_buffer_size = 512,
        .in_buffer_size = 512,
        .event_cb = on_event,
        .data_cb = on_rx,
        .user_arg = NULL,
    };
    esp_err_t err = cdc_acm_host_open(&cfg, &s_dev);
    if (err == ESP_ERR_NOT_FOUND || err == ESP_ERR_TIMEOUT) {
        s_dev = NULL;
        return fail(err, "nothing found on the USB port (check the OpenPort's cable, and 5 V on the port)");
    }
    if (err != ESP_OK) {
        s_dev = NULL;
        return fail(err, "could not open the USB device %04x:%04x as a serial port: %s", s_new_vid, s_new_pid,
                    esp_err_to_name(err));
    }
    s_vid = s_new_vid;
    s_pid = s_new_pid;
    // As pyserial does when openport.py opens the port: 115200 8N1, DTR and RTS on.
    cdc_acm_line_coding_t coding = {.dwDTERate = 115200, .bCharFormat = 0, .bParityType = 0, .bDataBits = 8};
    if ((err = cdc_acm_host_line_coding_set(s_dev, &coding)) != ESP_OK) {
        ESP_LOGW(TAG, "set line coding: %s (carrying on)", esp_err_to_name(err));
    }
    if ((err = cdc_acm_host_set_control_line_state(s_dev, true, true)) != ESP_OK) {
        ESP_LOGW(TAG, "set DTR/RTS: %s (carrying on)", esp_err_to_name(err));
    }
    vTaskDelay(pdMS_TO_TICKS(50));
    xStreamBufferReset(s_rx);
    s_len = 0;
    if ((err = command("\r\n\r\nati\r\n", "ari", 2000, NULL, 0, s_firmware, sizeof s_firmware)) != ESP_OK) {
        return err;
    }
    return command("ata\r\n", "aro", 2000, NULL, 0, NULL, 0);
}

uint16_t op_vid(void) { return s_vid; }
uint16_t op_pid(void) { return s_pid; }
const char *op_firmware(void) { return s_firmware; }
const char *op_last_error(void) { return s_error; }
uint32_t op_rx_overflows(void) { return s_overflows; }
uint32_t op_resyncs(void) { return s_resyncs; }
bool op_is_open(void) { return s_dev && !s_gone; }

void op_print_descriptors(void)
{
    if (s_dev) cdc_acm_host_desc_print(s_dev);
}

esp_err_t op_battery_mv(int *mv)
{
    char line[64];
    esp_err_t err = command("atr 16\r\n", "arr", 2000, NULL, 0, line, sizeof line);
    if (err != ESP_OK) return err;
    // "arr 16 <millivolts>"
    const char *p = strchr(line + 4, ' ');
    if (!p) return fail(ESP_ERR_INVALID_RESPONSE, "unexpected voltage reply '%s'", line);
    *mv = atoi(p + 1);
    return ESP_OK;
}

esp_err_t op_connect_can(void)
{
    char cmd[48];
    snprintf(cmd, sizeof cmd, "ato%d 0 500000 0\r\n", ISO15765);
    esp_err_t err = command(cmd, "aro", 2000, NULL, 0, NULL, 0);
    if (err != ESP_OK) return err;
    s_channel_open = true;
    static const uint32_t config[][2] = {
        {CFG_LOOPBACK, 0}, {CFG_ISO15765_BS, 0}, {CFG_ISO15765_STMIN, 0},
        {CFG_BS_TX, 0xFFFF}, {CFG_STMIN_TX, 0xFFFF}, {CFG_ISO15765_WFT_MAX, 0},
    };
    for (size_t i = 0; i < sizeof config / sizeof *config; i++) {
        snprintf(cmd, sizeof cmd, "ats%d %lu %lu\r\n", ISO15765, (unsigned long)config[i][0],
                 (unsigned long)config[i][1]);
        if ((err = command(cmd, "aro", 2000, NULL, 0, NULL, 0)) != ESP_OK) return err;
    }
    // Flow-control filter: mask, pattern (the ECU's id) and flow-control id (ours), 4 bytes each.
    const uint8_t filter[12] = {0xFF, 0xFF, 0xFF, 0xFF,
                                0, 0, ECU_ID >> 8, ECU_ID & 0xFF,
                                0, 0, TESTER_ID >> 8, TESTER_ID & 0xFF};
    snprintf(cmd, sizeof cmd, "atf%d %d %d 4\r\n", ISO15765, FLOW_CONTROL_FILTER, TX_ISO15765_FRAME_PAD);
    return command(cmd, "arf", 2000, filter, sizeof filter, NULL, 0);
}

int op_request(const uint8_t *payload, size_t len, uint8_t *reply, size_t cap, uint32_t timeout_ms)
{
    uint8_t out[300];
    char head[32];
    size_t data_len = 4 + len;
    int n = snprintf(head, sizeof head, "att%d %u %d\r\n", ISO15765, (unsigned)data_len, TX_ISO15765_FRAME_PAD);
    if (n + data_len > sizeof out) {
        fail(ESP_ERR_INVALID_SIZE, "request too long");
        return -1;
    }
    memcpy(out, head, n);
    const uint8_t id[4] = {0, 0, TESTER_ID >> 8, TESTER_ID & 0xFF};
    memcpy(out + n, id, 4);
    memcpy(out + n + 4, payload, len);
    s_queue_n = 0;
    if (write_all(out, n + data_len) != ESP_OK) return -1;

    int64_t deadline = now_ms() + timeout_ms;
    while (true) {
        while (s_queue_n) {                        // oldest first
            size_t mlen = s_queue_len[0];
            uint8_t msg[MSG_MAX];
            memcpy(msg, s_queue[0], mlen);
            for (int k = 1; k < s_queue_n; k++) {
                memcpy(s_queue[k - 1], s_queue[k], s_queue_len[k]);
                s_queue_len[k - 1] = s_queue_len[k];
            }
            s_queue_n--;
            uint32_t cid = (uint32_t)msg[0] << 24 | msg[1] << 16 | msg[2] << 8 | msg[3];
            if (mlen >= 4 && cid == ECU_ID) {
                size_t rlen = mlen - 4 < cap ? mlen - 4 : cap;
                memcpy(reply, msg + 4, rlen);
                return (int)rlen;
            }
        }
        if (s_gone) {
            fail(ESP_ERR_INVALID_STATE, "OpenPort unplugged");
            return -1;
        }
        item_t item = next_item();
        if (item == ITEM_PKT) {
            handle_packet();
            continue;
        }
        if (item == ITEM_LINE) {
            if (strncmp(s_line, "are", 3) == 0) {
                device_error(s_line, "a request");
                return -1;
            }
            continue;
        }
        int64_t left = deadline - now_ms();
        if (left <= 0) {
            fail(ESP_ERR_TIMEOUT, "no reply from the ECU");
            return -1;
        }
        fill(left < 20 ? (uint32_t)left : 20);
    }
}

void op_close(void)
{
    if (!s_dev) return;
    if (!s_gone) {
        if (s_channel_open) {
            char cmd[16];
            snprintf(cmd, sizeof cmd, "atc%d\r\n", ISO15765);
            command(cmd, "aro", 1000, NULL, 0, NULL, 0);
        }
        write_all((const uint8_t *)"atz\r\n", 5);
    }
    s_channel_open = false;
    cdc_acm_host_close(s_dev);
    s_dev = NULL;
}
