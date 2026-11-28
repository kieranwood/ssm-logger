// Host test: drive_log.c recording drives from the fake ECU into a folder on this computer.
//   python3 ../gen_params.py ../../definitions/log_defs.xml GEN/ssm_params.h
//   cc -std=c11 -Istub -IGEN -o /tmp/test_drive_log test_drive_log.c ../main/drive_log.c && /tmp/test_drive_log DIR
// Leaves DIR/drive_0001.raw (36 s), drive_0002.raw (with a 5 s dropout) and drive_0009.raw (cut
// short by a "power cut") for test_ssmraw.py to check.
#include "../main/openport.c"
#include "fake_openport.inc"
#include "../main/drive_log.h"

#include <sys/stat.h>

#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); return 1; } } while (0)

static char dir[256];

static int exists(const char *name)
{
    char p[512];
    struct stat st;
    snprintf(p, sizeof p, "%s/%s", dir, name);
    return stat(p, &st) == 0;
}

int main(int argc, char **argv)
{
    if (argc != 2) { printf("usage: test_drive_log EMPTY_DIR\n"); return 2; }
    snprintf(dir, sizeof dir, "%s", argv[1]);
    g_quiet = 0;
    srand(2);
    CHECK(op_usb_start() == ESP_OK);
    ecu_turnaround_us = 45000;                // ~22 reads a second, like the real car
    dl_init(dir, NULL);

    no_ecu = 1;                               // ignition off: one attempt, no drive
    CHECK(!dl_step());
    no_ecu = 0;

    ecu_reads_left = 800;                     // drive 1: ~36 s, then the ignition goes off
    CHECK(dl_step());
    CHECK(exists("drive_0001.raw") && !exists("drive_0001.part"));

    ecu_reads_left = 100;                     // key on for ~4.5 s: thrown away
    CHECK(dl_step());
    CHECK(!exists("drive_0002.raw") && !exists("drive_0002.part"));

    ecu_reads_left = 300;                     // drive 2: a 5 s dropout in the middle, one file
    dropout_reads = 300;
    dropout_us = 5000000;
    CHECK(dl_step());
    CHECK(exists("drive_0002.raw") && !exists("drive_0003.raw"));

    // a power cut: a .part file whose last sample is cut short is finished at the next start
    char src[512], dst[512];
    snprintf(src, sizeof src, "%s/drive_0001.raw", dir);
    snprintf(dst, sizeof dst, "%s/drive_0009.part", dir);
    FILE *in = fopen(src, "rb"), *out = fopen(dst, "wb");
    static uint8_t buf[1 << 20];
    size_t n = fread(buf, 1, sizeof buf, in);
    fwrite(buf, 1, n - 37, out);
    fclose(in);
    fclose(out);
    dl_init(dir, NULL);
    CHECK(exists("drive_0009.raw") && !exists("drive_0009.part"));
    printf("ok: drives recorded, short one discarded, dropout kept in one file, power cut recovered\n");
    return 0;
}
