#ifndef FRAME_ACK_H
#define FRAME_ACK_H

#include <stdint.h>

typedef struct {
    uint32_t magic;
    uint32_t kind;
    uint32_t frame_id;
} FrameAck;

typedef struct {
    uint32_t magic;
    uint32_t kind;
    uint32_t seq;
    uint32_t net_rtt_last_x10;
    uint32_t net_rtt_avg_x10;
    uint32_t reserved;
} FrameAckStats;

#define FRAME_ACK_MAGIC 0x4641434Bu
#define FRAME_ACK_KIND_NETWORK 1u
#define FRAME_ACK_KIND_DECODE 2u
#define FRAME_ACK_KIND_KEYFRAME_REQ 3u
#define FRAME_ACK_KIND_STATS 4u

static inline uint32_t frame_ack_rtt_ms_to_x10(double ms) {
    if (ms < 0.0) ms = 0.0;
    double scaled = ms * 10.0 + 0.5;
    if (scaled < 0.0) return 0u;
    if (scaled > 4294967295.0) return 0xFFFFFFFFu;
    return (uint32_t)scaled;
}

static inline double frame_ack_rtt_x10_to_ms(uint32_t x10) {
    return (double)x10 / 10.0;
}

static inline int frame_ack_stats_valid(const FrameAckStats *s) {
    if (!s) return 0;
    if (s->magic != FRAME_ACK_MAGIC) return 0;
    if (s->kind != FRAME_ACK_KIND_STATS) return 0;
    return 1;
}

#endif
