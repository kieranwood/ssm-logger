// Commands on the COM port, for reading drives off the board (fetch_drives.py speaks this):
//   ls            FILE <name> <bytes>  for each finished drive, then OK
//   get <name> [offset]  BEGIN <name> <bytes> <crc32>, DATA <offset> <base64> lines, END <name>
//                 (ABORT <name> if the computer sends anything meanwhile)
//   rm <name>     OK, or ERR <why>
//   df            DF <total bytes> <used bytes>
//   status        STATUS <what the logger is doing>, UPLOAD <what the uploader is doing>
//   set <key> <value>   ssid, pass, url or token (stored on the board); OK or ERR
//   config        CONFIG ssid=… url=… pass=set|unset token=set|unset
//   upload        try uploading now; OK
#include "console.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "config.h"
#include "drive_log.h"
#include "uploader.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

#define UART CONFIG_ESP_CONSOLE_UART_NUM

static const char *s_dir;
static void (*s_df)(size_t *total, size_t *used);

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, size_t n)   // the same CRC-32 as zlib
{
    crc = ~crc;
    while (n--) {
        crc ^= *p++;
        for (int k = 0; k < 8; k++) crc = crc >> 1 ^ (0xEDB88320 & -(crc & 1));
    }
    return ~crc;
}

static bool valid_name(const char *name)
{
    size_t n = strlen(name);
    return n > 10 && n < 40 && strncmp(name, "drive_", 6) == 0 && strcmp(name + n - 4, ".raw") == 0 &&
           !strchr(name, '/');
}

static void cmd_ls(void)
{
    DIR *d = opendir(s_dir);
    if (d) {
        struct dirent *e;
        char path[320];
        while ((e = readdir(d))) {
            if (!valid_name(e->d_name)) continue;
            struct stat st;
            snprintf(path, sizeof path, "%s/%s", s_dir, e->d_name);
            if (stat(path, &st) == 0) printf("FILE %s %ld\n", e->d_name, (long)st.st_size);
        }
        closedir(d);
    }
    printf("OK\n");
}

// Send a drive from byte `offset` on (fetch_drives.py resumes from where a damaged line was).
// Each line is written in one go and carries its offset; any input from the computer stops it.
static void cmd_get(const char *name, long offset)
{
    char path[320];
    if (!valid_name(name)) {
        printf("ERR bad name\n");
        return;
    }
    snprintf(path, sizeof path, "%s/%s", s_dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        printf("ERR no such drive\n");
        return;
    }
    static uint8_t chunk[57 * 16];
    uint32_t crc = 0;
    long size = 0;
    size_t n;
    while ((n = fread(chunk, 1, sizeof chunk, f)) > 0) {
        crc = crc32_update(crc, chunk, n);
        size += n;
    }
    printf("BEGIN %s %ld %08lx\n", name, size, (unsigned long)crc);
    fflush(stdout);
    if (offset < 0 || offset > size || fseek(f, offset, SEEK_SET) != 0) {
        fclose(f);
        printf("ERR bad offset\n");
        return;
    }
    char line[128];
    int count = 0;
    while ((n = fread(chunk, 1, 57, f)) > 0) {          // 57 bytes -> 76 base64 characters
        size_t olen, len;
        len = snprintf(line, sizeof line, "DATA %ld ", offset);
        mbedtls_base64_encode((unsigned char *)line + len, sizeof line - len - 1, &olen, chunk, n);
        len += olen;
        line[len++] = '\n';
        uart_write_bytes(UART, line, len);
        offset += n;
        if (++count % 16 == 0) {
            size_t waiting = 0;
            uart_get_buffered_data_len(UART, &waiting);
            if (waiting) {                              // the computer wants it to stop
                fclose(f);
                uart_wait_tx_done(UART, pdMS_TO_TICKS(1000));
                printf("ABORT %s\n", name);
                return;
            }
            vTaskDelay(1);                              // let the idle task run (task watchdog)
        }
    }
    fclose(f);
    uart_wait_tx_done(UART, pdMS_TO_TICKS(1000));
    printf("END %s\n", name);
}

static void handle(char *line)
{
    char *arg = strchr(line, ' ');
    if (arg) *arg++ = 0;
    if (strcmp(line, "ls") == 0) {
        cmd_ls();
    } else if (strcmp(line, "get") == 0 && arg) {
        char *off = strchr(arg, ' ');
        if (off) *off++ = 0;
        cmd_get(arg, off ? atol(off) : 0);
    } else if (strcmp(line, "rm") == 0 && arg) {
        char path[320];
        snprintf(path, sizeof path, "%s/%s", s_dir, arg);
        if (!valid_name(arg)) printf("ERR bad name\n");
        else if (remove(path) != 0) printf("ERR no such drive\n");
        else printf("OK\n");
    } else if (strcmp(line, "df") == 0) {
        size_t total = 0, used = 0;
        if (s_df) s_df(&total, &used);
        printf("DF %u %u\n", (unsigned)total, (unsigned)used);
    } else if (strcmp(line, "status") == 0) {
        printf("STATUS %s\nUPLOAD %s\n", dl_status(), uploader_status());
    } else if (strcmp(line, "set") == 0 && arg) {
        char *value = strchr(arg, ' ');
        if (value) *value++ = 0;
        esp_err_t err = config_set(arg, value ? value : "");
        if (err == ESP_OK) {
            printf("OK\n");
            uploader_kick();
        } else {
            printf("ERR %s\n", err == ESP_ERR_NOT_FOUND ? "keys: ssid, pass, url, token" : esp_err_to_name(err));
        }
    } else if (strcmp(line, "config") == 0) {
        logger_config_t c;
        config_load(&c);
        printf("CONFIG ssid=%s url=%s pass=%s token=%s\n", c.ssid, c.url, c.pass[0] ? "set" : "unset",
               c.token[0] ? "set" : "unset");
    } else if (strcmp(line, "upload") == 0) {
        uploader_kick();
        printf("OK\n");
    } else {
        printf("ERR commands: ls, get NAME, rm NAME, df, status, set KEY VALUE, config, upload\n");
    }
    fflush(stdout);
}

static void console_task(void *arg)
{
    char line[256];
    size_t n = 0;
    while (true) {
        uint8_t c;
        if (uart_read_bytes(UART, &c, 1, portMAX_DELAY) != 1) continue;
        if (c == '\r' || c == '\n') {
            line[n] = 0;
            if (n) handle(line);
            n = 0;
        } else if (n < sizeof line - 1) {
            line[n++] = c;
        }
    }
}

void console_start(const char *dir, void (*df)(size_t *total, size_t *used))
{
    s_dir = dir;
    s_df = df;
    uart_driver_install(UART, 1024, 4096, 0, NULL, 0);
    uart_vfs_dev_use_driver(UART);
    xTaskCreate(console_task, "console", 6144, NULL, 5, NULL);
}
