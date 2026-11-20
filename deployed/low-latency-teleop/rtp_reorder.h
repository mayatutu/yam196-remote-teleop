#ifndef RTP_REORDER_H
#define RTP_REORDER_H

#include <stdint.h>
#include <string.h>

#define RTP_REORDER_DEFAULT_CAPACITY 8
#define RTP_REORDER_MAX_CAPACITY 64
#define RTP_REORDER_PACKET_MAX 2048
#define RTP_REORDER_DEFAULT_WAIT_US 10000LL

typedef void (*rtp_reorder_emit_fn)(void *user, const uint8_t *packet, int len);
typedef void (*rtp_reorder_missing_fn)(void *user, uint16_t first_missing, uint16_t missing_count);

typedef struct { void *user; rtp_reorder_emit_fn emit; rtp_reorder_missing_fn missing; } RtpReorderHooks;
typedef struct { int used; uint16_t sequence; int len; uint8_t packet[RTP_REORDER_PACKET_MAX]; } RtpReorderSlot;
typedef struct {
    RtpReorderHooks hooks;
    RtpReorderSlot slots[RTP_REORDER_MAX_CAPACITY];
    int capacity;
    int64_t wait_us;
    int expected_ready;
    uint16_t expected_sequence;
    int64_t gap_started_us;
    int64_t gaps_interval, reordered_interval, missing_interval, late_drop_interval;
    int64_t wait_us_sum_interval, wait_us_max_interval;
} RtpReorder;

static inline uint16_t rtp_reorder_read_be16(const uint8_t *p) { return (uint16_t)(((uint16_t)p[0] << 8) | p[1]); }
static inline int rtp_reorder_is_ahead(uint16_t a, uint16_t b) { uint16_t d = (uint16_t)(a - b); return d > 0 && d < 0x8000u; }
static inline int rtp_reorder_find(const RtpReorder *r, uint16_t sequence) {
    for (int i = 0; i < r->capacity; i++) if (r->slots[i].used && r->slots[i].sequence == sequence) return i;
    return -1;
}
static inline int rtp_reorder_count(const RtpReorder *r) { int n = 0; for (int i = 0; i < r->capacity; i++) n += r->slots[i].used; return n; }
static inline int rtp_reorder_min_slot(const RtpReorder *r) {
    int best = -1; uint16_t best_distance = 0;
    for (int i = 0; i < r->capacity; i++) {
        if (!r->slots[i].used) continue;
        uint16_t d = (uint16_t)(r->slots[i].sequence - r->expected_sequence);
        if (d == 0 || d >= 0x8000u) continue;
        if (best < 0 || d < best_distance) { best = i; best_distance = d; }
    }
    return best;
}
static inline void rtp_reorder_emit_slot(RtpReorder *r, int i) {
    RtpReorderSlot *s = &r->slots[i];
    if (r->hooks.emit) r->hooks.emit(r->hooks.user, s->packet, s->len);
    s->used = 0;
    r->expected_sequence++;
}
static inline void rtp_reorder_drain(RtpReorder *r, int64_t now_us) {
    int emitted = 0, i;
    while ((i = rtp_reorder_find(r, r->expected_sequence)) >= 0) { rtp_reorder_emit_slot(r, i); emitted++; }
    if (emitted && r->gap_started_us) {
        int64_t wait = now_us - r->gap_started_us;
        if (wait < 0) wait = 0;
        r->reordered_interval += emitted;
        r->wait_us_sum_interval += wait;
        if (wait > r->wait_us_max_interval) r->wait_us_max_interval = wait;
    }
    if (rtp_reorder_count(r) == 0) r->gap_started_us = 0;
}
static inline void rtp_reorder_skip_missing(RtpReorder *r, int64_t now_us) {
    int i = rtp_reorder_min_slot(r); if (i < 0) return;
    uint16_t next = r->slots[i].sequence, missing = (uint16_t)(next - r->expected_sequence);
    if (missing) { r->missing_interval += missing; if (r->hooks.missing) r->hooks.missing(r->hooks.user, r->expected_sequence, missing); }
    if (r->gap_started_us) { int64_t wait = now_us - r->gap_started_us; if (wait < 0) wait = 0; r->wait_us_sum_interval += wait; if (wait > r->wait_us_max_interval) r->wait_us_max_interval = wait; }
    r->expected_sequence = next; r->gap_started_us = 0; rtp_reorder_drain(r, now_us);
    if (rtp_reorder_count(r)) r->gap_started_us = now_us;
}
static inline void rtp_reorder_init(RtpReorder *r, const RtpReorderHooks *hooks, int capacity, int64_t wait_us) {
    memset(r, 0, sizeof(*r));
    if (hooks) r->hooks = *hooks;
    r->capacity = (capacity >= 1 && capacity <= RTP_REORDER_MAX_CAPACITY) ? capacity : RTP_REORDER_DEFAULT_CAPACITY;
    r->wait_us = wait_us >= 0 ? wait_us : RTP_REORDER_DEFAULT_WAIT_US;
}
static inline void rtp_reorder_flush(RtpReorder *r) {
    if (!r) return;
    memset(r->slots, 0, sizeof(r->slots));
    r->expected_ready = 0;
    r->gap_started_us = 0;
}
static inline void rtp_reorder_input(RtpReorder *r, const uint8_t *packet, int len, int64_t now_us) {
    if (!r || !packet || len < 12 || len > RTP_REORDER_PACKET_MAX) return;
    uint16_t sequence = rtp_reorder_read_be16(packet + 2);
    if (!r->expected_ready) { r->expected_ready = 1; r->expected_sequence = sequence; if (r->hooks.emit) r->hooks.emit(r->hooks.user, packet, len); r->expected_sequence++; return; }
    if (sequence == r->expected_sequence) { if (r->hooks.emit) r->hooks.emit(r->hooks.user, packet, len); r->expected_sequence++; rtp_reorder_drain(r, now_us); return; }
    if (!rtp_reorder_is_ahead(sequence, r->expected_sequence)) { r->late_drop_interval++; return; }
    if (rtp_reorder_find(r, sequence) >= 0) return;
    int free_slot = -1; for (int i = 0; i < r->capacity; i++) if (!r->slots[i].used) { free_slot = i; break; }
    if (free_slot < 0) { rtp_reorder_skip_missing(r, now_us); for (int i = 0; i < r->capacity; i++) if (!r->slots[i].used) { free_slot = i; break; } if (free_slot < 0) { r->late_drop_interval++; return; } }
    RtpReorderSlot *s = &r->slots[free_slot]; s->used = 1; s->sequence = sequence; s->len = len; memcpy(s->packet, packet, (size_t)len);
    if (sequence == r->expected_sequence) {
        rtp_reorder_drain(r, now_us);
    } else if (!r->gap_started_us) {
        r->gap_started_us = now_us;
        r->gaps_interval++;
    }
}
static inline void rtp_reorder_poll(RtpReorder *r, int64_t now_us) { if (r && r->gap_started_us && now_us - r->gap_started_us >= r->wait_us) rtp_reorder_skip_missing(r, now_us); }
static inline void rtp_reorder_reset_interval_stats(RtpReorder *r) { r->gaps_interval = r->reordered_interval = r->missing_interval = r->late_drop_interval = 0; r->wait_us_sum_interval = r->wait_us_max_interval = 0; }

#endif
