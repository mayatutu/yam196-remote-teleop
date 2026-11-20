#ifndef VIDEO_FEC_H
#define VIDEO_FEC_H

#include <stdint.h>

#define VIDEO_FEC_EXT_ID 11
#define VIDEO_FEC_PT 127
#define VIDEO_FEC_VERSION 1
#define VIDEO_FEC_K_MAX 5
#define VIDEO_FEC_MAX_FUTURE 8
#define VIDEO_FEC_MAX_PENDING 16
#define VIDEO_FEC_MAX_PKT 1500
#define VIDEO_FEC_HE_BYTES 12
#define VIDEO_FEC_RTP_H265_PT 100

typedef struct {
    int enabled;
    int k;
    int timeout_ms;
} VideoFecConfig;

void video_fec_parse_env(VideoFecConfig *cfg);
uint32_t video_fec_ssrc_from_media(uint32_t media_ssrc);

typedef struct {
    int enabled;
    int k;
    uint32_t media_ssrc;
    uint32_t fec_ssrc;
    uint16_t fec_seq;
    uint16_t block_id;
    int index;
    uint32_t first_ts;
    uint16_t lengths[VIDEO_FEC_K_MAX];
    uint8_t xor_buf[VIDEO_FEC_MAX_PKT];
    int xor_len;
    int64_t fec_sent;
} VideoFecSender;

void video_fec_sender_init(VideoFecSender *s, const VideoFecConfig *cfg, uint32_t media_ssrc);

/* Insert HE, XOR-accumulate, optionally produce FEC RTP when block completes. */
int video_fec_sender_on_media(VideoFecSender *s,
                              const uint8_t *pkt,
                              int bytes,
                              uint32_t timestamp,
                              uint8_t *out_media,
                              int *out_media_len,
                              int out_cap,
                              uint8_t *out_fec,
                              int *out_fec_len,
                              int fec_cap);

/* KEYFRAME truncate: emit FEC with k_actual=index if index>0. */
int video_fec_sender_flush(VideoFecSender *s, uint8_t *out_fec, int *out_fec_len, int fec_cap);

typedef void (*video_fec_emit_fn)(void *user, const uint8_t *pkt, int len);
typedef void (*video_fec_deep_fn)(void *user, const char *reason);

typedef struct {
    void *user;
    video_fec_emit_fn emit;
    video_fec_deep_fn deep;
    int *rtp_seq_ready;
    uint16_t *last_rtp_seq;
} VideoFecRecvHooks;

typedef struct {
    int present;
    uint16_t seq;
    int len;
    uint8_t data[VIDEO_FEC_MAX_PKT];
} VideoFecSlot;

typedef struct {
    int in_use;
    uint16_t block_id;
    int k_from_media;
    uint16_t first_seq;
    int has_first_seq;
    int has_received_fec;
    int fec_k;
    uint16_t fec_lengths[VIDEO_FEC_K_MAX];
    uint8_t fec_xor[VIDEO_FEC_MAX_PKT];
    int fec_xor_len;
    VideoFecSlot slots[VIDEO_FEC_K_MAX];
    int received_count;
    int64_t first_enqueue_us;
    int unrecoverable_closing; /* emit_partial 期间：缺失 seq 不可再视为可恢复 */
} VideoFecBlock;

typedef struct {
    int64_t fec_recv;
    int64_t fec_recv_version_drop;
    int64_t fec_recovered;
    int64_t fec_fail;
    int64_t fec_conflict;
    int64_t fec_block_closed_by_new_block;
    int64_t fec_wait_us_sum;
    int64_t fec_wait_us_max;
    int64_t fec_closed_blocks;
} VideoFecStats;

typedef struct {
    int enabled;
    int k;
    int timeout_ms;
    int media_ssrc_known;
    uint32_t media_ssrc;
    uint32_t fec_ssrc;
    VideoFecBlock blocks[VIDEO_FEC_MAX_PENDING];
    uint16_t last_closed;
    int has_last_closed;
    VideoFecRecvHooks hooks;
    VideoFecStats stats;
} VideoFecReceiver;

void video_fec_receiver_init(VideoFecReceiver *r, const VideoFecConfig *cfg, const VideoFecRecvHooks *hooks);
VideoFecReceiver *video_fec_receiver_create(const VideoFecConfig *cfg, const VideoFecRecvHooks *hooks);
void video_fec_receiver_destroy(VideoFecReceiver *r);
void video_fec_receiver_input(VideoFecReceiver *r, const uint8_t *pkt, int len, int64_t now_us);
void video_fec_receiver_poll_timeouts(VideoFecReceiver *r, int64_t now_us);
int video_fec_seq_recoverable(const VideoFecReceiver *r, uint16_t seq);
void video_fec_receiver_reset_interval_stats(VideoFecReceiver *r);

#endif /* VIDEO_FEC_H */
