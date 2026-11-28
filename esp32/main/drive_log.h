// Recording drives: wait for the ECU, record raw SSM replies to a file until the ignition goes off.
//
// File format (.raw; little-endian), read by ../../ssmraw.py:
//   "SSMRAW1\n"                      magic
//   u8  init_len, init[init_len]     the ECU's init reply after the 0xEA byte (its ID and capabilities)
//   u16 n_addr, u32 addr[n_addr]     the addresses read, in the order of each sample's bytes
//   u32 start_unix                   wall-clock start (0 if the clock wasn't set)
//   then one record per sample:  u32 t_ms (since the start), u8 data[n_addr]
// A record cut short by a power cut is ignored when reading.
//
// While a drive is being written it is drive_NNNN.part; it becomes drive_NNNN.raw when the drive
// ends, or is deleted if the engine ran for less than 10 s (the key turned to ON and back).
#pragma once

#include <stdbool.h>
#include <stddef.h>

// dir: where drives are kept. free_bytes: space left there (NULL: don't check).
// Finishes any .part files left by a power cut.
void dl_init(const char *dir, size_t (*free_bytes)(void));

// One go: try to reach the ECU; if it answers, record a whole drive and return true.
// Returns false straight away (after one attempt) if the ECU didn't answer.
bool dl_step(void);

// Called with the OpenPort's OBD voltage (mV) each time the ECU connects (NULL: nothing).
void dl_on_connect(void (*cb)(int obd_mv));

// What it's doing, in words (for the console).
const char *dl_status(void);
