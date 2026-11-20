#include "rtp_loss_recovery.h"

#include <stdio.h>

static int failed;
#define EXPECT(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failed++; } } while (0)

static void test_note_and_take(void) {
    RtpLossRecovery s;
    uint16_t first = 0, count = 0;
    rtp_loss_recovery_init(&s, 1);
    EXPECT(s.decode_ok == 1);
    EXPECT(rtp_loss_recovery_take(&s, &first, &count) == 0);
    rtp_loss_recovery_note_missing(&s, 101, 1);
    EXPECT(s.decode_ok == 0);
    EXPECT(s.pending == 1);
    EXPECT(rtp_loss_recovery_take(&s, &first, &count) == 1);
    EXPECT(first == 101 && count == 1);
    EXPECT(s.pending == 0);
    EXPECT(rtp_loss_recovery_take(&s, &first, &count) == 0);
    EXPECT(s.decode_ok == 0);
}

static void test_merge_counts(void) {
    RtpLossRecovery s;
    uint16_t first = 0, count = 0;
    rtp_loss_recovery_init(&s, 1);
    rtp_loss_recovery_note_missing(&s, 200, 3);
    rtp_loss_recovery_note_missing(&s, 210, 11);
    EXPECT(rtp_loss_recovery_take(&s, &first, &count) == 1);
    EXPECT(first == 200 && count == 14);
}

int main(void) {
    test_note_and_take();
    test_merge_counts();
    if (failed) return 1;
    puts("rtp_loss_recovery_selftest: ok");
    return 0;
}
