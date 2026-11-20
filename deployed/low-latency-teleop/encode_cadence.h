#ifndef ENCODE_CADENCE_H
#define ENCODE_CADENCE_H

#include <stdint.h>

/* Monotonic encode deadline. Accept the newest capture when now >= next_at,
 * then advance to the next future slot. Never burst-catch-up after a stall. */
static inline int encode_cadence_accept(int64_t *next_at, int64_t now, int64_t interval) {
    if (!next_at || interval <= 0) return 1;
    if (*next_at == 0) {
        *next_at = now + interval;
        return 1;
    }
    if (now < *next_at) return 0;
    *next_at += interval;
    while (*next_at <= now) *next_at += interval;
    return 1;
}

#endif
