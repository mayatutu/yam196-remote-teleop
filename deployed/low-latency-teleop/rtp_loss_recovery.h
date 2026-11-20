#ifndef RTP_LOSS_RECOVERY_H
#define RTP_LOSS_RECOVERY_H

#include <stdint.h>

typedef struct {
    int pending;
    uint16_t first;
    uint16_t count;
    int decode_ok;
} RtpLossRecovery;

static inline void rtp_loss_recovery_init(RtpLossRecovery *s, int decode_ok) {
    if (!s) return;
    s->pending = 0;
    s->first = 0;
    s->count = 0;
    s->decode_ok = decode_ok;
}

static inline void rtp_loss_recovery_note_missing(RtpLossRecovery *s, uint16_t first, uint16_t count) {
    if (!s || count == 0) return;
    if (!s->pending) {
        s->pending = 1;
        s->first = first;
        s->count = count;
    } else if ((uint32_t)s->count + count > 0xFFFFu) {
        s->count = 0xFFFFu;
    } else {
        s->count = (uint16_t)(s->count + count);
    }
    s->decode_ok = 0;
}

static inline int rtp_loss_recovery_take(RtpLossRecovery *s, uint16_t *first, uint16_t *count) {
    if (!s || !s->pending) return 0;
    if (first) *first = s->first;
    if (count) *count = s->count;
    s->pending = 0;
    s->count = 0;
    return 1;
}

#endif
