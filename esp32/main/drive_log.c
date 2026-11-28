// See drive_log.h. Mirrors record_drive / finish_drive in ../../car_logger.py.
#include "drive_log.h"

#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "openport.h"
#include "ssm_params.h"

static const char *TAG = "drive";

#define MAGIC "SSMRAW1\n"
#define MAX_ADDR 255
#define MAX_PER_READ 84               // the most the 2012 Forester ECU accepts in one read (sources.py)
#define MAX_REQ ((MAX_ADDR + MAX_PER_READ - 1) / MAX_PER_READ)
#define END_AFTER_MS 15000            // ECU silent this long: the drive is over
#define MIN_ENGINE_MS 10000           // keep a drive only if the engine ran this long
#define READ_FAILURES 5               // failed reads in a row before the ECU counts as gone
#define ENGINE_RUNNING_RPM 300
#define FLUSH_MS 1000                 // at most this much is lost if the power goes
#define LOW_SPACE_BYTES (32 * 1024)
#define CLOCK_SET_AFTER 1600000000     // time() past this (Sept 2020): the clock has been set
#define PATH_LEN 460                  // folder (up to 199) + '/' + file name (up to 255)

static char s_dir[200];
static char s_status[128] = "starting";
static size_t (*s_free_bytes)(void);
static char s_last_wait[160];

static struct {
    uint8_t init[255];
    size_t init_len;
    char ecu[11];
    int mv;
    uint32_t addr[MAX_ADDR];
    int n_addr;
    uint8_t req[MAX_REQ][2 + 3 * MAX_PER_READ];
    size_t req_len[MAX_REQ];
    int req_count[MAX_REQ];           // addresses in each request
    int n_req;
    int i_rpm_hi, i_rpm_lo;           // where Engine Speed (0x0E-0x0F) is in a sample, or -1
} S;

static uint8_t s_buf[8192];           // samples waiting to be written
static size_t s_buf_len;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static void put_u16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static void put_u32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static uint32_t get_u32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

// The addresses to read for the parameters this ECU supports: defs.supported_by + request_addresses.
static void build_requests(void)
{
    S.n_addr = 0;
    for (size_t i = 0; i < SSM_PARAM_COUNT; i++) {
        const ssm_param_t *p = &SSM_PARAMS[i];
        size_t idx = p->support_byte + 7;
        if (idx >= S.init_len || !(S.init[idx] & (1 << (p->support_bit - 1)))) continue;
        for (int k = 0; k < p->length; k++) {
            uint32_t a = p->address + k;
            bool seen = false;
            for (int j = 0; j < S.n_addr && !seen; j++) seen = S.addr[j] == a;
            if (!seen && S.n_addr < MAX_ADDR) S.addr[S.n_addr++] = a;
        }
    }
    S.i_rpm_hi = S.i_rpm_lo = -1;
    for (int j = 0; j < S.n_addr; j++) {
        if (S.addr[j] == 0x0E) S.i_rpm_hi = j;
        if (S.addr[j] == 0x0F) S.i_rpm_lo = j;
    }
    S.n_req = 0;
    for (int start = 0; start < S.n_addr; start += MAX_PER_READ) {
        int count = S.n_addr - start < MAX_PER_READ ? S.n_addr - start : MAX_PER_READ;
        uint8_t *r = S.req[S.n_req];
        r[0] = 0xA8;                  // SSM read addresses
        r[1] = 0x00;                  // single read
        for (int j = 0; j < count; j++) {
            uint32_t a = S.addr[start + j];
            r[2 + 3 * j] = a >> 16;
            r[3 + 3 * j] = a >> 8;
            r[4 + 3 * j] = a;
        }
        S.req_len[S.n_req] = 2 + 3 * count;
        S.req_count[S.n_req] = count;
        S.n_req++;
    }
}

// Open the OpenPort and the ECU. first: a new drive (take the ECU's parameter list from this init).
static bool connect_ecu(bool first)
{
    if (op_open(2000) != ESP_OK) return false;
    S.mv = 0;
    op_battery_mv(&S.mv);
    if (op_connect_can() != ESP_OK) {
        op_close();
        return false;
    }
    uint8_t r[300];
    const uint8_t init = 0xAA;
    int n = op_request(&init, 1, r, sizeof r, 2000);
    if (n < 9 || r[0] != 0xEA) {
        op_close();
        return false;
    }
    char ecu[11];
    for (int i = 0; i < 5; i++) snprintf(ecu + 2 * i, 3, "%02X", r[4 + i]);
    if (first) {
        S.init_len = (size_t)(n - 1) < sizeof S.init ? (size_t)(n - 1) : sizeof S.init;
        memcpy(S.init, r + 1, S.init_len);
        memcpy(S.ecu, ecu, sizeof ecu);
        build_requests();
    } else if (strcmp(ecu, S.ecu) != 0) {
        ESP_LOGW(TAG, "a different ECU (%s) answered - ending the drive", ecu);
        op_close();
        return false;
    }
    return true;
}

static bool read_sample(uint8_t *out)
{
    uint8_t r[300];
    size_t off = 0;
    for (int k = 0; k < S.n_req; k++) {
        int n = op_request(S.req[k], S.req_len[k], r, sizeof r, 500);
        if (n != S.req_count[k] + 1 || r[0] != 0xE8) return false;
        memcpy(out + off, r + 1, S.req_count[k]);
        off += S.req_count[k];
    }
    return true;
}

static bool flush(FILE *f)
{
    if (s_buf_len && fwrite(s_buf, 1, s_buf_len, f) != s_buf_len) return false;
    s_buf_len = 0;
    return fflush(f) == 0 && fsync(fileno(f)) == 0;
}

// The next drive number. It keeps counting up after uploaded drives are deleted (remembered in
// <dir>/next_drive), so the same number never names two drives on the server.
static int next_number(void)
{
    int best = 0;
    DIR *d = opendir(s_dir);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d))) {
            int n;
            if (sscanf(e->d_name, "drive_%d.", &n) == 1 && n > best) best = n;
        }
        closedir(d);
    }
    char path[PATH_LEN];
    snprintf(path, sizeof path, "%s/next_drive", s_dir);
    int saved = 0;
    FILE *f = fopen(path, "r");
    if (f) {
        if (fscanf(f, "%d", &saved) != 1) saved = 0;
        fclose(f);
    }
    int num = best + 1 > saved ? best + 1 : saved;
    if ((f = fopen(path, "w"))) {
        fprintf(f, "%d\n", num + 1);
        fflush(f);
        fsync(fileno(f));
        fclose(f);
    }
    return num;
}

// How long the engine ran in a recording (from its Engine Speed bytes); false if unreadable.
static bool file_engine_ms(const char *path, uint32_t *engine_ms, uint32_t *samples)
{
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    uint8_t hdr[8 + 1 + 255 + 2];
    bool ok = fread(hdr, 1, 9, f) == 9 && memcmp(hdr, MAGIC, 8) == 0;
    uint8_t init_len = hdr[8];
    uint8_t nb[2];
    ok = ok && fread(hdr, 1, init_len, f) == init_len && fread(nb, 1, 2, f) == 2;
    int n = ok ? nb[0] | nb[1] << 8 : 0;
    int hi = -1, lo = -1;
    for (int j = 0; ok && j < n; j++) {
        uint8_t a[4];
        ok = fread(a, 1, 4, f) == 4;
        if (get_u32(a) == 0x0E) hi = j;
        if (get_u32(a) == 0x0F) lo = j;
    }
    uint8_t start[4];
    ok = ok && fread(start, 1, 4, f) == 4 && n > 0;
    *engine_ms = 0;
    *samples = 0;
    if (ok) {
        uint8_t rec[4 + MAX_ADDR];
        int64_t last = -1;
        while (fread(rec, 1, 4 + n, f) == (size_t)(4 + n)) {
            uint32_t t = get_u32(rec);
            int rpm = hi >= 0 && lo >= 0 ? (rec[4 + hi] << 8 | rec[4 + lo]) / 4 : ENGINE_RUNNING_RPM + 1;
            if (last >= 0 && rpm > ENGINE_RUNNING_RPM && t - last < 2000) *engine_ms += t - last;
            last = t;
            (*samples)++;
        }
    }
    fclose(f);
    return ok;
}

// Rename a finished .part to .raw, or delete it if the engine never ran long enough.
static void finish(const char *part, uint32_t engine_ms, uint32_t samples)
{
    char raw[PATH_LEN];
    snprintf(raw, sizeof raw, "%.*s.raw", (int)(strlen(part) - 5), part);
    if (engine_ms < MIN_ENGINE_MS) {
        ESP_LOGI(TAG, "Discarding %s: the engine ran for %.0f s", part, engine_ms / 1000.0);
        remove(part);
        return;
    }
    if (rename(part, raw) != 0) {
        ESP_LOGE(TAG, "Could not rename %s", part);
        return;
    }
    ESP_LOGI(TAG, "Saved %s (%lu samples, engine running %.1f min)", raw, (unsigned long)samples,
             engine_ms / 60000.0);
}

void dl_init(const char *dir, size_t (*free_bytes)(void))
{
    snprintf(s_dir, sizeof s_dir, "%s", dir);
    s_free_bytes = free_bytes;
    DIR *d = opendir(s_dir);
    if (!d) return;
    char parts[8][PATH_LEN];
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d)) && n < 8) {
        size_t len = strlen(e->d_name);
        if (len > 5 && strcmp(e->d_name + len - 5, ".part") == 0) {
            snprintf(parts[n++], sizeof parts[0], "%s/%s", s_dir, e->d_name);
        }
    }
    closedir(d);
    for (int i = 0; i < n; i++) {
        uint32_t engine_ms, samples;
        ESP_LOGI(TAG, "Recovering %s from before a power cut", parts[i]);
        if (file_engine_ms(parts[i], &engine_ms, &samples)) {
            finish(parts[i], engine_ms, samples);
        } else {
            ESP_LOGW(TAG, "%s is unreadable - deleting it", parts[i]);
            remove(parts[i]);
        }
    }
}

const char *dl_status(void) { return s_status; }

static void record_drive(void)
{
    char part[PATH_LEN];
    int num = next_number();
    snprintf(part, sizeof part, "%s/drive_%04d.part", s_dir, num);
    FILE *f = fopen(part, "wb");
    if (!f) {
        ESP_LOGE(TAG, "Could not create %s", part);
        snprintf(s_status, sizeof s_status, "error: could not create a file");
        op_close();
        vTaskDelay(pdMS_TO_TICKS(5000));
        return;
    }
    // header
    uint8_t h[8 + 1 + 255 + 2 + 4 * MAX_ADDR + 4];
    size_t hl = 0;
    memcpy(h, MAGIC, 8);
    hl = 8;
    h[hl++] = S.init_len;
    memcpy(h + hl, S.init, S.init_len);
    hl += S.init_len;
    put_u16(h + hl, S.n_addr);
    hl += 2;
    for (int j = 0; j < S.n_addr; j++, hl += 4) put_u32(h + hl, S.addr[j]);
    time_t wall = time(NULL);
    bool have_start = wall > CLOCK_SET_AFTER;
    const long start_pos = hl;
    put_u32(h + hl, have_start ? (uint32_t)wall : 0);   // 0 until the clock has been set (Wi-Fi)
    hl += 4;
    s_buf_len = 0;
    bool ok = fwrite(h, 1, hl, f) == hl && flush(f);

    ESP_LOGI(TAG, "Ignition on (ECU %s, %.2f V): recording %s, %d addresses in %d request(s)", S.ecu,
             S.mv / 1000.0, part, S.n_addr, S.n_req);
    const size_t rec_len = 4 + S.n_addr;
    int64_t t0 = now_ms(), last_flush = t0, last_report = t0;
    int64_t last_t = -1;
    uint32_t engine_ms = 0, samples = 0, report_samples = 0;
    int failures = 0;
    uint8_t data[MAX_ADDR];
    while (ok) {
        if (read_sample(data)) {
            failures = 0;
            int64_t now = now_ms();
            uint32_t t = now - t0;
            put_u32(s_buf + s_buf_len, t);
            memcpy(s_buf + s_buf_len + 4, data, S.n_addr);
            s_buf_len += rec_len;
            samples++;
            int rpm = S.i_rpm_hi >= 0 && S.i_rpm_lo >= 0 ? (data[S.i_rpm_hi] << 8 | data[S.i_rpm_lo]) / 4
                                                          : ENGINE_RUNNING_RPM + 1;
            if (last_t >= 0 && rpm > ENGINE_RUNNING_RPM && t - last_t < 2000) engine_ms += t - last_t;
            last_t = t;
            if (now - last_flush >= FLUSH_MS || s_buf_len + rec_len > sizeof s_buf) {
                if (!flush(f)) {
                    ESP_LOGE(TAG, "Writing %s failed (flash full?) - ending the drive", part);
                    break;
                }
                last_flush = now;
                if (!have_start && time(NULL) > CLOCK_SET_AFTER) {
                    // The clock was set during the drive (home Wi-Fi in range): fill in the start time.
                    uint8_t st[4];
                    put_u32(st, (uint32_t)(time(NULL) - (now - t0) / 1000));
                    have_start = fseek(f, start_pos, SEEK_SET) == 0 && fwrite(st, 1, 4, f) == 4;
                    fseek(f, 0, SEEK_END);
                    if (have_start) ESP_LOGI(TAG, "Clock set: %s start time filled in", part);
                }
                if (s_free_bytes && s_free_bytes() < LOW_SPACE_BYTES) {
                    ESP_LOGE(TAG, "Flash full - ending the drive");
                    break;
                }
            }
            if (now - last_report >= 60000) {
                ESP_LOGI(TAG, "Recording %s: %lu samples, %.1f/s, engine %.1f min, %u kB free", part,
                         (unsigned long)samples, (samples - report_samples) * 1000.0 / (now - last_report),
                         engine_ms / 60000.0, s_free_bytes ? (unsigned)(s_free_bytes() / 1024) : 0);
                last_report = now;
                report_samples = samples;
            }
            snprintf(s_status, sizeof s_status, "recording drive_%04d: %lu samples, engine %.1f min", num,
                     (unsigned long)samples, engine_ms / 60000.0);
            continue;
        }
        if (++failures < READ_FAILURES && op_is_open()) continue;
        // The ECU stopped answering: the ignition went off, or a dropout. Give it END_AFTER_MS to come back.
        if (!flush(f)) break;
        ESP_LOGI(TAG, "ECU stopped answering (%s) - waiting %d s", op_last_error(), END_AFTER_MS / 1000);
        op_close();
        int64_t lost = now_ms();
        bool back = false;
        while (now_ms() - lost < END_AFTER_MS) {
            if ((back = connect_ecu(false))) break;
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
        if (!back) break;
        ESP_LOGI(TAG, "ECU back after %.0f s - carrying on with %s", (now_ms() - lost) / 1000.0, part);
        failures = 0;
    }
    flush(f);
    fclose(f);
    op_close();
    finish(part, engine_ms, samples);
}

bool dl_step(void)
{
    if (!connect_ecu(true)) {
        const char *why = op_last_error();
        if (strcmp(why, s_last_wait) != 0) {
            ESP_LOGI(TAG, "Waiting for the ignition: %s", why);
            snprintf(s_last_wait, sizeof s_last_wait, "%s", why);
        }
        snprintf(s_status, sizeof s_status, "waiting for the ignition (%s)", why);
        return false;
    }
    s_last_wait[0] = 0;
    record_drive();
    snprintf(s_status, sizeof s_status, "waiting for the ignition");
    return true;
}
