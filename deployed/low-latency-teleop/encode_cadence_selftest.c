#include "encode_cadence.h"

#include <stdio.h>

static int failed;
#define EXPECT(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failed++; } } while (0)

static void test_90_to_30(void) {
    const int64_t interval = 1000000 / 30;
    const int64_t capture = 1000000 / 90;
    int64_t next = 0;
    int accepted = 0;
    for (int i = 0; i < 90; i++) {
        int64_t now = (int64_t)i * capture;
        if (encode_cadence_accept(&next, now, interval)) accepted++;
    }
    EXPECT(accepted >= 29 && accepted <= 31);
}

static void test_no_burst_after_stall(void) {
    const int64_t interval = 33333;
    int64_t next = 0;
    EXPECT(encode_cadence_accept(&next, 1000, interval) == 1);
    EXPECT(next == 1000 + interval);
    EXPECT(encode_cadence_accept(&next, 2000, interval) == 0);
    EXPECT(encode_cadence_accept(&next, 1000 + interval + 50000, interval) == 1);
    EXPECT(next > 1000 + interval + 50000);
    EXPECT(encode_cadence_accept(&next, 1000 + interval + 50001, interval) == 0);
}

int main(void) {
    test_90_to_30();
    test_no_burst_after_stall();
    if (failed) return 1;
    puts("encode_cadence_selftest: ok");
    return 0;
}
