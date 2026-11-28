// The wake decision, on its own so it can be tested on a computer (test/test_wake_rule.c).
#pragma once

#include <stdbool.h>

#define WAKE_RISE_MV 400        // the battery must be this far above its lowest reading since sleeping
#define WAKE_CHECKS 2           // ...on this many checks in a row (a spike doesn't wake it)

// One check while asleep. min_mv: lowest reading since going to sleep; above: checks in a row that
// passed. Returns true when the engine is running and the board should boot.
static inline bool wake_check(int mv, int wake_mv, int *min_mv, int *above)
{
    if (mv < *min_mv) *min_mv = mv;
    if (mv < wake_mv || mv < *min_mv + WAKE_RISE_MV) {
        *above = 0;
        return false;
    }
    if (++*above < WAKE_CHECKS) return false;
    *above = 0;
    return true;
}
