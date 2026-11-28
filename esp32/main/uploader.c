// Every 30 s (or when kicked): if drives are waiting, or the clock hasn't been set yet, turn Wi-Fi on
// and try the home network. Once connected: set the clock over NTP, check log_server.py is there
// (GET /health), upload each drive (PUT /upload/<name>, with its SHA-256) and delete it once the
// server's copy matches. Then turn Wi-Fi off again. It runs during drives too: with a charger that
// only has power with the ignition on, the board is off once parked, so it uploads as you leave.
#include "uploader.h"

#include <dirent.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "config.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

static const char *TAG = "upload";

#define CHECK_EVERY_MS 30000
#define CONNECT_TIMEOUT_MS 15000
#define CLOCK_SET_AFTER 1600000000
#define GOT_IP BIT0
#define GAVE_UP BIT1
#define MAX_PENDING 32

static const char *s_dir;
static char s_status[160] = "starting";
static TaskHandle_t s_task;
static EventGroupHandle_t s_events;
static int s_retries;
static bool s_sntp_started;
static bool s_wifi_on;

static void set_status(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void set_status(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_status, sizeof s_status, fmt, ap);
    va_end(ap);
}

const char *uploader_status(void) { return s_status; }

void uploader_kick(void)
{
    if (s_task) xTaskNotifyGive(s_task);
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retries++ < 2) esp_wifi_connect();
        else xEventGroupSetBits(s_events, GAVE_UP);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_retries = 0;
        xEventGroupSetBits(s_events, GOT_IP);
    }
}

static bool wifi_up(const logger_config_t *c)
{
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, c->ssid, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, c->pass, sizeof wc.sta.password);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    xEventGroupClearBits(s_events, GOT_IP | GAVE_UP);
    s_retries = 0;
    if (esp_wifi_start() != ESP_OK) return false;
    s_wifi_on = true;
    EventBits_t bits = xEventGroupWaitBits(s_events, GOT_IP | GAVE_UP, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(CONNECT_TIMEOUT_MS));
    return bits & GOT_IP;
}

static void wifi_down(void)
{
    if (s_wifi_on) {
        esp_wifi_disconnect();
        esp_wifi_stop();
        s_wifi_on = false;
    }
}

static int pending(char names[][32], int cap)
{
    int n = 0;
    DIR *d = opendir(s_dir);
    if (!d) return 0;
    struct dirent *e;
    while ((e = readdir(d)) && n < cap) {
        size_t len = strlen(e->d_name);
        if (strncmp(e->d_name, "drive_", 6) == 0 && len > 4 && len < 32 && strcmp(e->d_name + len - 4, ".raw") == 0) {
            strcpy(names[n++], e->d_name);
        }
    }
    closedir(d);
    qsort(names, n, 32, (int (*)(const void *, const void *))strcmp);    // oldest first
    return n;
}

static esp_http_client_handle_t client_for(const logger_config_t *c, const char *path, esp_http_client_method_t m,
                                           int timeout_ms)
{
    char url[256];
    snprintf(url, sizeof url, "%s%s", c->url, path);
    esp_http_client_config_t hc = {.url = url, .method = m, .timeout_ms = timeout_ms};
    esp_http_client_handle_t h = esp_http_client_init(&hc);
    if (h) {
        char auth[128];
        snprintf(auth, sizeof auth, "Bearer %s", c->token);
        esp_http_client_set_header(h, "Authorization", auth);
        esp_http_client_set_header(h, "User-Agent", "ssm-esp32");
    }
    return h;
}

static bool server_ok(const logger_config_t *c)
{
    esp_http_client_handle_t h = client_for(c, "/health", HTTP_METHOD_GET, 5000);
    if (!h) return false;
    esp_err_t err = esp_http_client_perform(h);
    int status = err == ESP_OK ? esp_http_client_get_status_code(h) : 0;
    esp_http_client_cleanup(h);
    if (status == 200) return true;
    if (status == 401) set_status("the server refused the token - check `set token`");
    else if (status) set_status("the server answered %d to /health", status);
    else set_status("server %s not reachable (%s)", c->url, esp_err_to_name(err));
    return false;
}

static bool upload(const logger_config_t *c, const char *name)
{
    char path[320];
    snprintf(path, sizeof path, "%s/%s", s_dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    static uint8_t buf[4096];
    uint8_t digest[32];
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    long size = 0;
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) {
        mbedtls_sha256_update(&sha, buf, n);
        size += n;
    }
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);
    char hex[65];
    for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);

    char where[64];
    snprintf(where, sizeof where, "/upload/%s", name);
    esp_http_client_handle_t h = client_for(c, where, HTTP_METHOD_PUT, 60000);
    bool ok = false;
    int64_t t0 = esp_timer_get_time();
    if (h) {
        esp_http_client_set_header(h, "Content-Type", "application/octet-stream");
        esp_http_client_set_header(h, "X-Content-SHA256", hex);
        if (esp_http_client_open(h, size) == ESP_OK) {
            rewind(f);
            bool sent = true;
            while (sent && (n = fread(buf, 1, sizeof buf, f)) > 0) {
                for (size_t off = 0; off < n;) {
                    int w = esp_http_client_write(h, (const char *)buf + off, n - off);
                    if (w <= 0) {
                        sent = false;
                        break;
                    }
                    off += w;
                }
            }
            if (sent && esp_http_client_fetch_headers(h) >= 0) {
                int status = esp_http_client_get_status_code(h);
                char reply[384] = {0};
                esp_http_client_read_response(h, reply, sizeof reply - 1);
                ok = status == 201 && strstr(reply, hex);
                if (!ok) {
                    ESP_LOGW(TAG, "%s: the server answered %d: %.120s", name, status, reply);
                    set_status("%s: the server answered %d", name, status);
                }
            } else {
                set_status("%s: upload cut off", name);
            }
        } else {
            set_status("%s: could not connect to %s", name, c->url);
        }
        esp_http_client_cleanup(h);
    }
    fclose(f);
    if (ok) {
        remove(path);        // the server has a verified copy
        ESP_LOGI(TAG, "Uploaded %s (%.2f MB in %.1f s)", name, size / 1e6, (esp_timer_get_time() - t0) / 1e6);
    }
    return ok;
}

static void upload_task(void *arg)
{
    static char names[MAX_PENDING][32];
    while (true) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(CHECK_EVERY_MS));
        logger_config_t c;
        config_load(&c);
        if (!c.ssid[0]) {
            set_status("Wi-Fi not set up (set ssid / pass / url / token)");
            continue;
        }
        int n = pending(names, MAX_PENDING);
        bool clock_set = time(NULL) > CLOCK_SET_AFTER;
        if (!n && clock_set) {
            set_status("nothing to upload");
            continue;
        }
        if (!wifi_up(&c)) {
            set_status("%s not in range%s", c.ssid, n ? " - drives wait for the next try" : "");
            wifi_down();
            continue;
        }
        if (!s_sntp_started) {
            esp_sntp_config_t sc = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
            s_sntp_started = esp_netif_sntp_init(&sc) == ESP_OK;
        }
        if (!clock_set && esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) == ESP_OK) {
            ESP_LOGI(TAG, "Clock set over NTP");
        }
        if (n && !c.url[0]) {
            set_status("on %s, but no server url (set url)", c.ssid);
        } else if (n && server_ok(&c)) {
            int done = 0;
            for (int i = 0; i < n && upload(&c, names[i]); i++) done++;
            if (done == n) set_status("all drives uploaded");
            else if (done) ESP_LOGW(TAG, "Uploaded %d of %d drives: %s", done, n, s_status);
        } else if (n) {
            ESP_LOGW(TAG, "%d drive(s) waiting: %s", n, s_status);
        } else {
            set_status("nothing to upload");
        }
        wifi_down();
    }
}

void uploader_start(const char *dir)
{
    s_dir = dir;
    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_wifi_set_storage(WIFI_STORAGE_RAM);       // the settings live in our own NVS namespace
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL);
    xTaskCreate(upload_task, "upload", 8192, NULL, 3, &s_task);
}
