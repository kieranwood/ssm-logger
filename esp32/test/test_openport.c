// Host test: openport.c against a fake OpenPort that replies the way the web demo's emulator does,
// delivered in random-sized chunks with junk, TX-done packets and multi-packet messages mixed in.
#include "../main/openport.c"
#include <assert.h>

int g_quiet = 1;
int64_t g_now_us = 0;

// ---- receive FIFO (the "stream buffer"), handed out in random-sized pieces
static uint8_t fifo[1 << 16]; static size_t fhead, ftail;
struct sb { int x; } the_sb;
StreamBufferHandle_t xStreamBufferCreate(size_t s, size_t t) { (void)s; (void)t; return &the_sb; }
size_t xStreamBufferSend(StreamBufferHandle_t b, const void *d, size_t n, unsigned w) {
    (void)b; (void)w; memcpy(fifo + ftail, d, n); ftail += n; return n; }
size_t xStreamBufferReceive(StreamBufferHandle_t b, void *d, size_t n, unsigned wait_ms) {
    (void)b;
    size_t avail = ftail - fhead;
    if (!avail) { g_now_us += (int64_t)wait_ms * 1000; return 0; }
    size_t k = 1 + rand() % 13; if (k > avail) k = avail; if (k > n) k = n;
    memcpy(d, fifo + fhead, k); fhead += k; g_now_us += 100; return k; }
void xStreamBufferReset(StreamBufferHandle_t b) { (void)b; fhead = ftail = 0; }

// ---- fake device
static const usb_device_desc_t desc = {0x0403, 0xCC4C, 0x02};
esp_err_t usb_host_get_device_descriptor(usb_device_handle_t d, const usb_device_desc_t **out) { (void)d; *out = &desc; return 0; }
esp_err_t cdc_acm_host_install(const cdc_acm_host_driver_config_t *c) { c->new_dev_cb(NULL); return 0; }
static int plugged = 1, no_ecu = 0, junk = 1, corrupt_next = 0;
esp_err_t cdc_acm_host_open(const cdc_acm_host_open_config_t *c, cdc_acm_dev_hdl_t *h) {
    (void)c; if (!plugged) return ESP_ERR_NOT_FOUND; *h = (cdc_acm_dev_hdl_t)1; return 0; }
esp_err_t cdc_acm_host_close(cdc_acm_dev_hdl_t h) { (void)h; return 0; }
esp_err_t cdc_acm_host_line_coding_set(cdc_acm_dev_hdl_t h, const cdc_acm_line_coding_t *c) { (void)h; (void)c; return 0; }
esp_err_t cdc_acm_host_set_control_line_state(cdc_acm_dev_hdl_t h, bool a, bool b) { (void)h; (void)a; (void)b; return 0; }
void cdc_acm_host_desc_print(cdc_acm_dev_hdl_t h) { (void)h; }

static void out(const void *d, size_t n) { on_rx(d, n, NULL); }
static void outs(const char *s) { if (junk && rand() % 3 == 0) out("zz", 2); out(s, strlen(s)); }
static void packet(uint8_t type, const uint8_t *msg, size_t n) {
    uint8_t p[300] = {'a', 'r', '6', (uint8_t)(5 + n), type, 0, 0, 0, 0};
    memcpy(p + 9, msg, n);
    size_t total = 9 + n;
    if (corrupt_next) { corrupt_next = 0; out(p, total - 3); return; }     // lose the end of a packet
    out(p, total); }
static const uint8_t INIT[] = {0xa2,0x10,0x0a,0x71,0x52,0x34,0x50,0x07,0xf3,0xfa,0xc9,0x8c,0x02,0x02,0xfe,0xa8,0x00,0x00,0x00,0x46,0xce,0x54,0xfd,0xb8,0x00,0x00,0x0f,0x20,0x00,0x00,0x00,0x00,0x00,0xdc,0x00,0x00,0x5d,0x1f,0x30,0x80,0xf0,0x24,0x10,0x00,0x43,0xfb,0x00,0xf5,0xc9,0x8e,0x00,0x00,0x00,0x01,0x21,0xf1,0x80,0x00,0xb1};
static int tx_count, reads;
static void ecu_reply(const uint8_t *payload, size_t n) {
    uint8_t txdone[4] = {0, 0, 0x07, 0xE0};
    packet(0x10, txdone, 4);                                              // TX done: ignored
    if (no_ecu) return;
    uint8_t msg[300] = {0, 0, 0x07, 0xE8}; size_t m = 4;
    if (n == 1 && payload[0] == 0xAA) { msg[m++] = 0xEA; memcpy(msg + m, INIT, sizeof INIT); m += sizeof INIT; }
    else if (payload[0] == 0xA8) { const uint8_t r[] = {0xE8, 0x17, 0x70, 0x82, 0xB3}; memcpy(msg + m, r, 5); m += 5; reads++; }
    if (m > 40) {                                    // long message: two packets, the CAN id repeated
        packet(0x00, msg, 40);
        uint8_t rest[300] = {0, 0, 0x07, 0xE8}; memcpy(rest + 4, msg + 40, m - 40);
        packet(0x40, rest, 4 + m - 40);
    } else {
        packet(0x40, msg, m);
    }
}
static uint8_t in[4096]; static size_t inlen;
esp_err_t cdc_acm_host_data_tx_blocking(cdc_acm_dev_hdl_t h, const uint8_t *d, size_t n, uint32_t t) {
    (void)h; (void)t; memcpy(in + inlen, d, n); inlen += n;
    while (true) {
        uint8_t *e = NULL;
        for (size_t i = 0; i + 1 < inlen; i++) if (in[i] == '\r' && in[i + 1] == '\n') { e = in + i; break; }
        if (!e) break;
        size_t ll = e - in; char line[128]; memcpy(line, in, ll); line[ll] = 0;
        size_t used = ll + 2, bin = 0;
        if (!strncmp(line, "att6", 4)) bin = atoi(line + 5);
        else if (!strncmp(line, "atf6", 4)) bin = 12;
        if (inlen < used + bin) break;
        const uint8_t *b = in + used;
        if (!ll) {}
        else if (!strncmp(line, "ati", 3)) outs("ari Fake OpenPort 2.0 1.17\r\n");
        else if (!strncmp(line, "atr 16", 6)) outs("arr 16 12450\r\n");
        else if (!strncmp(line, "atf6", 4)) {
            assert(!strcmp(line, "atf6 3 64 4"));
            const uint8_t want[12] = {0xff,0xff,0xff,0xff,0,0,7,0xe8,0,0,7,0xe0}; assert(!memcmp(b, want, 12));
            outs("arf6 0\r\n"); }
        else if (!strncmp(line, "att6", 4)) {
            assert(strstr(line, " 64")); assert(b[2] == 7 && b[3] == 0xE0);
            tx_count++; ecu_reply(b + 4, bin - 4); }
        else if (!strncmp(line, "atz", 3)) {}
        else outs("aro\r\n");
        memmove(in, in + used + bin, inlen - used - bin); inlen -= used + bin;
    }
    return 0; }

#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s (%s)\n", __LINE__, #c, op_last_error()); return 1; } } while (0)
int main(void) {
    srand(1);
    CHECK(op_usb_start() == ESP_OK);
    for (int round = 0; round < 300; round++) {
        CHECK(op_open(1000) == ESP_OK);
        CHECK(op_vid() == 0x0403 && op_pid() == 0xCC4C);
        CHECK(!strcmp(op_firmware(), "ari Fake OpenPort 2.0 1.17"));
        int mv; CHECK(op_battery_mv(&mv) == ESP_OK && mv == 12450);
        CHECK(op_connect_can() == ESP_OK);
        uint8_t r[300]; uint8_t aa = 0xAA;
        int n = op_request(&aa, 1, r, sizeof r, 2000);
        CHECK(n == 1 + (int)sizeof INIT && r[0] == 0xEA && !memcmp(r + 1, INIT, sizeof INIT));
        CHECK(r[4] == 0x71 && r[8] == 0x07);
        const uint8_t rd[] = {0xA8, 0, 0, 0, 0x0E, 0, 0, 0x0F, 0, 0, 0x08, 0, 0, 0x1C};
        for (int i = 0; i < 20; i++) {
            n = op_request(rd, sizeof rd, r, sizeof r, 500);
            CHECK(n == 5 && r[0] == 0xE8 && ((r[1] << 8 | r[2]) / 4) == 1500 && r[3] - 40 == 90);
        }
        op_close();
    }
    printf("ok: 300 sessions x 20 reads with random chunking and junk\n");

    // a packet that loses bytes: the next one must still be found (resync), the read after succeeds
    CHECK(op_open(1000) == ESP_OK && op_connect_can() == ESP_OK);
    const uint8_t rd[] = {0xA8, 0, 0, 0, 0x0E, 0, 0, 0x0F, 0, 0, 0x08, 0, 0, 0x1C};
    uint8_t r[300];
    int good = 0;
    for (int i = 0; i < 50; i++) { if (i % 5 == 0) corrupt_next = 1; if (op_request(rd, sizeof rd, r, sizeof r, 500) == 5) good++; }
    printf("ok: with every 5th packet damaged, %d/50 reads came back, %lu resyncs\n", good, (unsigned long)op_resyncs());
    CHECK(good >= 30);
    CHECK(op_request(rd, sizeof rd, r, sizeof r, 500) == 5);

    // no ECU (ignition off): the request times out, doesn't hang
    no_ecu = 1; int64_t t = g_now_us;
    uint8_t aa = 0xAA;
    CHECK(op_request(&aa, 1, r, sizeof r, 2000) == -1);
    printf("ok: no ECU -> timed out after %.1f s: %s\n", (g_now_us - t) / 1e6, op_last_error());
    no_ecu = 0; op_close();

    // unplugged
    plugged = 0;
    CHECK(op_open(1000) != ESP_OK);
    printf("ok: nothing plugged in -> %s\n", op_last_error());
    plugged = 1;
    // unplugged mid-session
    CHECK(op_open(1000) == ESP_OK && op_connect_can() == ESP_OK);
    on_event(&(cdc_acm_host_dev_event_data_t){.type = CDC_ACM_HOST_DEVICE_DISCONNECTED}, NULL);
    CHECK(op_request(rd, sizeof rd, r, sizeof r, 500) == -1 && !op_is_open());
    printf("ok: unplugged mid-session -> %s\n", op_last_error());
    op_close();
    printf("%d requests sent\n", tx_count);
    return 0;
}
