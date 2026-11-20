/*
 * FFmpeg-free FEC unit self-test.
 * Build: make fec_selftest && ./fec_selftest
 */
#include "fec.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_fail;

#define EXPECT(cond)                                                                                                   \
    do {                                                                                                               \
        if (!(cond)) {                                                                                                 \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                                             \
            g_fail++;                                                                                                  \
        }                                                                                                              \
    } while (0)

typedef struct {
    uint8_t pkts[16][VIDEO_FEC_MAX_PKT];
    int lens[16];
    int count;
    int deep_count;
    int rtp_seq_ready;
    uint16_t last_rtp_seq;
} TestSink;

static void test_emit(void *user, const uint8_t *pkt, int len) {
    TestSink *s = (TestSink *)user;
    if (s->count >= 16 || len <= 0 || len > VIDEO_FEC_MAX_PKT) return;
    memcpy(s->pkts[s->count], pkt, (size_t)len);
    s->lens[s->count] = len;
    s->count++;
}

static void test_deep(void *user, const char *reason) {
    (void)reason;
    ((TestSink *)user)->deep_count++;
}

static void write_be16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xff);
}

static void write_be32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)(v & 0xff);
}

static uint16_t read_be16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static void make_media_rtp(uint8_t *out, int *out_len, uint16_t seq, uint32_t ts, uint32_t ssrc, const uint8_t *pay, int pay_len) {
    out[0] = 0x80;
    out[1] = (uint8_t)VIDEO_FEC_RTP_H265_PT;
    write_be16(out + 2, seq);
    write_be32(out + 4, ts);
    write_be32(out + 8, ssrc);
    memcpy(out + 12, pay, (size_t)pay_len);
    *out_len = 12 + pay_len;
}

static void test_ssrc_map(void) {
    EXPECT(video_fec_ssrc_from_media(0x12345678u) == (0x12345678u ^ 0xFFFFFFFFu));
    EXPECT(video_fec_ssrc_from_media(0xFFFFFFFFu) == 1u);
}

static void test_sender_disabled_passthrough(void) {
    VideoFecConfig cfg = {.enabled = 0, .k = 3, .timeout_ms = 50};
    VideoFecSender s;
    video_fec_sender_init(&s, &cfg, 0x11111111u);
    uint8_t in[20];
    int in_len = 0;
    uint8_t pay[] = {1, 2, 3, 4};
    make_media_rtp(in, &in_len, 1, 90000, 0x11111111u, pay, 4);
    uint8_t out[64], fec[64];
    int ol = 0, fl = 0;
    EXPECT(video_fec_sender_on_media(&s, in, in_len, 90000, out, &ol, 64, fec, &fl, 64) == 0);
    EXPECT(ol == in_len);
    EXPECT(fl == 0);
    EXPECT(memcmp(out, in, (size_t)in_len) == 0);
    EXPECT(!(out[0] & 0x10));
}

static void test_sender_he_and_full_block_fec(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender s;
    video_fec_sender_init(&s, &cfg, 0xABCDEF01u);
    uint8_t media[3][VIDEO_FEC_MAX_PKT];
    int mlen[3] = {0};
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int flen = 0;

    for (int i = 0; i < 3; i++) {
        uint8_t raw[64];
        int raw_len = 0;
        uint8_t pay[8];
        memset(pay, (uint8_t)(0x10 + i), sizeof(pay));
        make_media_rtp(raw, &raw_len, (uint16_t)(100 + i), 90000, 0xABCDEF01u, pay, 8);
        uint8_t mout[VIDEO_FEC_MAX_PKT];
        int mol = 0;
        flen = 0;
        EXPECT(video_fec_sender_on_media(&s, raw, raw_len, 90000, mout, &mol, (int)sizeof(mout), fec, &flen, (int)sizeof(fec)) ==
               0);
        EXPECT(mol == raw_len + VIDEO_FEC_HE_BYTES);
        EXPECT(mout[0] & 0x10);
        EXPECT(mout[16] == 0xB4);
        EXPECT(mout[19] == (uint8_t)i);
        EXPECT(mout[20] == 3);
        memcpy(media[i], mout, (size_t)mol);
        mlen[i] = mol;
        if (i < 2) EXPECT(flen == 0);
    }
    EXPECT(flen > 12);
    EXPECT((fec[1] & 0x7F) == VIDEO_FEC_PT);
    EXPECT(fec[12] == VIDEO_FEC_VERSION);
    EXPECT(fec[13] == 3);
    EXPECT(s.fec_sent == 1);
    EXPECT(s.index == 0);
    EXPECT(s.block_id == 1);
}

static void test_keyframe_flush_truncate(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender s;
    video_fec_sender_init(&s, &cfg, 0x10u);
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int flen = 0;
    EXPECT(video_fec_sender_flush(&s, fec, &flen, (int)sizeof(fec)) == 0);
    EXPECT(flen == 0);

    uint8_t raw[32], mout[64];
    int raw_len = 0, mol = 0;
    uint8_t pay[] = {9, 9};
    make_media_rtp(raw, &raw_len, 1, 1, 0x10u, pay, 2);
    EXPECT(video_fec_sender_on_media(&s, raw, raw_len, 1, mout, &mol, 64, fec, &flen, (int)sizeof(fec)) == 0);
    EXPECT(flen == 0);
    EXPECT(s.index == 1);
    flen = 0;
    EXPECT(video_fec_sender_flush(&s, fec, &flen, (int)sizeof(fec)) == 0);
    EXPECT(flen > 12);
    EXPECT(fec[13] == 1); /* k_actual = 1 */
    EXPECT(s.index == 0);
    EXPECT(s.block_id == 1);
}

static void test_recover_one_missing(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender snd;
    video_fec_sender_init(&snd, &cfg, 0x55u);

    uint8_t media[3][VIDEO_FEC_MAX_PKT];
    int mlen[3] = {0};
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int flen = 0;
    for (int i = 0; i < 3; i++) {
        uint8_t raw[40];
        int raw_len = 0;
        uint8_t pay[4] = {(uint8_t)i, 1, 2, 3};
        make_media_rtp(raw, &raw_len, (uint16_t)(10 + i), 777, 0x55u, pay, 4);
        int mol = 0;
        flen = 0;
        EXPECT(video_fec_sender_on_media(&snd, raw, raw_len, 777, media[i], &mol, VIDEO_FEC_MAX_PKT, fec, &flen,
                                         VIDEO_FEC_MAX_PKT) == 0);
        mlen[i] = mol;
    }
    EXPECT(flen > 0);

    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);

    /* drop media[1], deliver 0, 2, then FEC */
    video_fec_receiver_input(rcv, media[0], mlen[0], 1000);
    video_fec_receiver_input(rcv, media[2], mlen[2], 1001);
    EXPECT(sink.count == 0);
    EXPECT(video_fec_seq_recoverable(rcv, 11) == 1);
    EXPECT(video_fec_seq_recoverable(rcv, 99) == 0);
    video_fec_receiver_input(rcv, fec, flen, 1002);
    EXPECT(sink.count == 3);
    EXPECT(rcv->stats.fec_recovered == 1);
    EXPECT(sink.deep_count == 0);
    /* emitted packets must be stripped (no X) */
    for (int i = 0; i < sink.count; i++) {
        EXPECT(!(sink.pkts[i][0] & 0x10));
        EXPECT((sink.pkts[i][1] & 0x7F) == VIDEO_FEC_RTP_H265_PT);
    }
    EXPECT(read_be16(sink.pkts[0] + 2) == 10);
    EXPECT(read_be16(sink.pkts[1] + 2) == 11);
    EXPECT(read_be16(sink.pkts[2] + 2) == 12);
    video_fec_receiver_destroy(rcv);
}

static void test_full_block_immediate_close(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender snd;
    video_fec_sender_init(&snd, &cfg, 0x66u);
    uint8_t media[3][VIDEO_FEC_MAX_PKT];
    int mlen[3];
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int flen = 0;
    for (int i = 0; i < 3; i++) {
        uint8_t raw[32];
        int rl = 0;
        uint8_t pay[2] = {7, (uint8_t)i};
        make_media_rtp(raw, &rl, (uint16_t)i, 1, 0x66u, pay, 2);
        flen = 0;
        EXPECT(video_fec_sender_on_media(&snd, raw, rl, 1, media[i], &mlen[i], VIDEO_FEC_MAX_PKT, fec, &flen,
                                         VIDEO_FEC_MAX_PKT) == 0);
    }

    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);
    video_fec_receiver_input(rcv, media[0], mlen[0], 1);
    video_fec_receiver_input(rcv, media[1], mlen[1], 2);
    video_fec_receiver_input(rcv, media[2], mlen[2], 3);
    EXPECT(sink.count == 3);
    EXPECT(sink.deep_count == 0);
    EXPECT(rcv->has_last_closed == 1);
    EXPECT(rcv->last_closed == 0);
    video_fec_receiver_destroy(rcv);
}

static void test_version_drop_and_unknown_pt(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);

    uint8_t bad_fec[32];
    memset(bad_fec, 0, sizeof(bad_fec));
    bad_fec[0] = 0x80;
    bad_fec[1] = VIDEO_FEC_PT;
    write_be32(bad_fec + 8, video_fec_ssrc_from_media(1));
    bad_fec[12] = 99; /* bad version */
    bad_fec[13] = 1;
    video_fec_receiver_input(rcv, bad_fec, 20, 1);
    EXPECT(rcv->stats.fec_recv == 0);
    EXPECT(rcv->stats.fec_recv_version_drop == 1);

    uint8_t unk[16];
    memset(unk, 0, sizeof(unk));
    unk[0] = 0x80;
    unk[1] = 96; /* unknown PT */
    write_be32(unk + 8, 1);
    video_fec_receiver_input(rcv, unk, 16, 2);
    EXPECT(sink.count == 0);
    video_fec_receiver_destroy(rcv);
}

static void test_hint_close_no_fec_sync_max_only(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender snd;
    video_fec_sender_init(&snd, &cfg, 0x77u);

    /* build two partial media for block 0 (index 0 only), then start block 1 */
    uint8_t m0[VIDEO_FEC_MAX_PKT], m1[VIDEO_FEC_MAX_PKT], fec[VIDEO_FEC_MAX_PKT];
    int l0 = 0, l1 = 0, fl = 0;
    uint8_t raw[40];
    int rl = 0;
    uint8_t pay[] = {1};
    make_media_rtp(raw, &rl, 50, 1, 0x77u, pay, 1);
    EXPECT(video_fec_sender_on_media(&snd, raw, rl, 1, m0, &l0, VIDEO_FEC_MAX_PKT, fec, &fl, VIDEO_FEC_MAX_PKT) == 0);
    EXPECT(video_fec_sender_flush(&snd, fec, &fl, VIDEO_FEC_MAX_PKT) == 0); /* truncate block 0 */
    make_media_rtp(raw, &rl, 51, 2, 0x77u, pay, 1);
    fl = 0;
    EXPECT(video_fec_sender_on_media(&snd, raw, rl, 2, m1, &l1, VIDEO_FEC_MAX_PKT, fec, &fl, VIDEO_FEC_MAX_PKT) == 0);

    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    sink.rtp_seq_ready = 1;
    sink.last_rtp_seq = 49;
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);

    /* Deliver only media of block1 first? Actually deliver m0 then m1 without FEC for block0.
       m0 has block_id=0; after flush sender block_id=1; m1 has block_id=1.
       If we only send m0 then m1 (no FEC for either yet), m1 should hint-close block0. */
    video_fec_receiver_input(rcv, m0, l0, 10);
    EXPECT(sink.count == 0);
    video_fec_receiver_input(rcv, m1, l1, 11);
    /* block0 hint-closed with 1 packet → emit + deep */
    EXPECT(sink.count >= 1);
    EXPECT(sink.deep_count >= 1);
    EXPECT(sink.last_rtp_seq == 50); /* max emitted only, not assuming k=3 tail */
    EXPECT(rcv->stats.fec_block_closed_by_new_block >= 1);
    video_fec_receiver_destroy(rcv);
}

static void test_missing_ge2_not_recoverable(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender snd;
    video_fec_sender_init(&snd, &cfg, 0x88u);
    uint8_t media[3][VIDEO_FEC_MAX_PKT];
    int mlen[3];
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int fl = 0;
    for (int i = 0; i < 3; i++) {
        uint8_t raw[32];
        int rl = 0;
        uint8_t pay[1] = {(uint8_t)i};
        make_media_rtp(raw, &rl, (uint16_t)(20 + i), 1, 0x88u, pay, 1);
        fl = 0;
        video_fec_sender_on_media(&snd, raw, rl, 1, media[i], &mlen[i], VIDEO_FEC_MAX_PKT, fec, &fl, VIDEO_FEC_MAX_PKT);
    }

    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);
    video_fec_receiver_input(rcv, media[0], mlen[0], 1);
    /* missing 21 and 22 */
    EXPECT(video_fec_seq_recoverable(rcv, 21) == 0);
    EXPECT(video_fec_seq_recoverable(rcv, 22) == 0);
    video_fec_receiver_destroy(rcv);
}

static void test_timeout_empty_advances_last_closed(void) {
    VideoFecConfig cfg = {.enabled = 1, .k = 3, .timeout_ms = 50};
    VideoFecSender snd;
    video_fec_sender_init(&snd, &cfg, 0x99u);
    uint8_t media[3][VIDEO_FEC_MAX_PKT];
    int mlen[3];
    uint8_t fec[VIDEO_FEC_MAX_PKT];
    int fl = 0;
    for (int i = 0; i < 3; i++) {
        uint8_t raw[32];
        int rl = 0;
        uint8_t pay[] = {(uint8_t)i};
        make_media_rtp(raw, &rl, (uint16_t)(i + 1), 1, 0x99u, pay, 1);
        fl = 0;
        video_fec_sender_on_media(&snd, raw, rl, 1, media[i], &mlen[i], VIDEO_FEC_MAX_PKT, fec, &fl, VIDEO_FEC_MAX_PKT);
    }
    EXPECT(fl > 0);

    TestSink sink;
    memset(&sink, 0, sizeof(sink));
    VideoFecRecvHooks hooks = {
        .user = &sink,
        .emit = test_emit,
        .deep = test_deep,
        .rtp_seq_ready = &sink.rtp_seq_ready,
        .last_rtp_seq = &sink.last_rtp_seq,
    };
    VideoFecReceiver *rcv = video_fec_receiver_create(&cfg, &hooks);
    EXPECT(rcv != NULL);
    /* FEC only, k=3, 0 media → cannot recover; timeout closes empty */
    video_fec_receiver_input(rcv, fec, fl, 1000);
    EXPECT(sink.count == 0);
    video_fec_receiver_poll_timeouts(rcv, 1000 + 50 * 1000 + 1);
    EXPECT(rcv->has_last_closed == 1);
    EXPECT(sink.count == 0);
    EXPECT(sink.deep_count == 0);
    video_fec_receiver_destroy(rcv);
}

int main(void) {
    test_ssrc_map();
    test_sender_disabled_passthrough();
    test_sender_he_and_full_block_fec();
    test_keyframe_flush_truncate();
    test_recover_one_missing();
    test_full_block_immediate_close();
    test_version_drop_and_unknown_pt();
    test_hint_close_no_fec_sync_max_only();
    test_missing_ge2_not_recoverable();
    test_timeout_empty_advances_last_closed();

    if (g_fail) {
        fprintf(stderr, "fec_selftest: %d failure(s)\n", g_fail);
        return 1;
    }
    printf("fec_selftest: ok\n");
    return 0;
}
