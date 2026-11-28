// A fake OpenPort with a fake ECU behind it, for the host tests. Include it after openport.c
// (it feeds replies straight into openport.c's USB receive callback). Replies come the way the web
// demo's emulator (web/fake-ecu.js) sends them, in random-sized pieces with junk mixed in.
#include <assert.h>

int g_quiet = 1;
int64_t g_now_us = 0;

// ---- receive FIFO (the "stream buffer"), handed out in random-sized pieces
static uint8_t fifo[1 << 16]; static size_t fhead, ftail;
struct sb { int x; } the_sb;
StreamBufferHandle_t xStreamBufferCreate(size_t s, size_t t) { (void)s; (void)t; return &the_sb; }
size_t xStreamBufferSend(StreamBufferHandle_t b, const void *d, size_t n, unsigned w) {
    (void)b; (void)w;
    if (fhead == ftail) fhead = ftail = 0;
    if (ftail + n > sizeof fifo) { memmove(fifo, fifo + fhead, ftail - fhead); ftail -= fhead; fhead = 0; }
    memcpy(fifo + ftail, d, n); ftail += n; return n; }
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
static int plugged = 1, no_ecu = 0, junk = 1, corrupt_next = 0, no_ecu_now = 0;
static long ecu_reads_left = -1;        // A8 reads the ECU answers before the ignition goes off (-1: forever)
static int64_t silent_until_us = 0;     // the ECU says nothing until then (a dropout)
static int64_t ecu_turnaround_us = 0;
static long dropout_reads = 0;          // after ecu_reads_left runs out: a dropout, then this many more
static int64_t dropout_us = 0;
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
    g_now_us += ecu_turnaround_us;                   // the ECU takes a while to answer
    if (ecu_reads_left == 0 && dropout_reads > 0) {   // go quiet for dropout_us, then answer again
        silent_until_us = g_now_us + dropout_us; ecu_reads_left = dropout_reads; dropout_reads = 0; }
    if (ecu_reads_left == 0 || g_now_us < silent_until_us) no_ecu_now = 1; else no_ecu_now = 0;
    if (no_ecu || no_ecu_now) return;
    if (ecu_reads_left > 0 && payload[0] == 0xA8) ecu_reads_left--;
    uint8_t msg[300] = {0, 0, 0x07, 0xE8}; size_t m = 4;
    if (n == 1 && payload[0] == 0xAA) { msg[m++] = 0xEA; memcpy(msg + m, INIT, sizeof INIT); m += sizeof INIT; }
    else if (payload[0] == 0xA8) {                   // one byte per address asked for
        msg[m++] = 0xE8;
        for (size_t k = 2; k + 3 <= n; k += 3) {
            uint32_t a = (uint32_t)payload[k] << 16 | payload[k + 1] << 8 | payload[k + 2];
            msg[m++] = a == 0x0E ? 0x17 : a == 0x0F ? 0x70 : a == 0x08 ? 0x82 : a == 0x1C ? 0xB3 : (uint8_t)(a * 7);
        }
        reads++;
    }
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

