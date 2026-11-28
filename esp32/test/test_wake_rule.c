// Host test for the wake decision (main/wake_rule.h) against battery voltage traces, one reading
// every 5 s as the board takes them asleep.
//   cc -std=c11 -Wall -o /tmp/test_wake_rule test_wake_rule.c && /tmp/test_wake_rule
#include <stdio.h>
#include "../main/wake_rule.h"

#define WAKE 13200

// Feed readings from the moment it went to sleep (at the first reading); return the index of the
// reading that woke it, or -1.
static int run(const int *mv, int n)
{
    int min_mv = mv[0], above = 0;
    for (int i = 1; i < n; i++)
        if (wake_check(mv[i], WAKE, &min_mv, &above)) return i;
    return -1;
}

static int fails;
static void expect(const char *what, int got, int want)
{
    printf("%s %s: woke at reading %d\n", got == want ? "ok:" : "FAIL:", what, got);
    fails += got != want;
}

int main(void)
{
    int t[200], n;

    // Engine off: surface charge 13.4 V falling to 12.6 V over ~10 min. Must never wake.
    n = 0;
    for (int v = 13400; v >= 12600; v -= 7) t[n++] = v;
    expect("battery settling after the engine stops", run(t, n), -1);

    // ...then the engine starts: 14.2 V. Wakes on the second check.
    t[n++] = 14200; t[n++] = 14200; t[n++] = 14200;
    expect("engine started after it settled", run(t, n), n - 2);

    // A short stop: asleep at 13.3 V, restarted 2 minutes later while still at 13.25 V.
    n = 0;
    for (int i = 0; i < 24; i++) t[n++] = 13300 - i * 2;
    t[n++] = 14100; t[n++] = 14150;
    expect("restarted before the battery settled (still above 13.2 V)", run(t, n), n - 1);

    // A single spike (a charger in the garage, a door motor): must not wake.
    n = 0;
    for (int i = 0; i < 20; i++) t[n++] = 12600;
    t[n++] = 13700;
    for (int i = 0; i < 20; i++) t[n++] = 12600;
    expect("one high reading", run(t, n), -1);

    // A battery charger connected while parked (13.6 V and staying there): wakes, which is fine:
    // the ECU won't answer, so it goes back to sleep after a minute and won't wake again for the
    // same level (it is the new lowest).
    n = 0;
    for (int i = 0; i < 10; i++) t[n++] = 12500;
    for (int i = 0; i < 10; i++) t[n++] = 13600;
    expect("battery charger connected", run(t, n), 11);
    n = 0;
    for (int i = 0; i < 30; i++) t[n++] = 13600;
    expect("...asleep again on the charger", run(t, n), -1);

    // Ignition on, engine off (12.3 V): no wake (key-on-only sessions are discarded anyway).
    n = 0;
    for (int i = 0; i < 30; i++) t[n++] = 12300;
    expect("ignition on, engine off", run(t, n), -1);
    return fails ? 1 : 0;
}
