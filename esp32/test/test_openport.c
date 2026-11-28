// Host test: openport.c against a fake OpenPort that replies the way the web demo's emulator does,
// delivered in random-sized chunks with junk, TX-done packets and multi-packet messages mixed in.
#include "../main/openport.c"
#include "fake_openport.inc"

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
