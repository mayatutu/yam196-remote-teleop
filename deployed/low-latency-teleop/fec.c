#include "fec.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint16_t read_be16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t read_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
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

uint32_t video_fec_ssrc_from_media(uint32_t media_ssrc) {
    uint32_t s = media_ssrc ^ 0xFFFFFFFFu;
    return s ? s : 1u;
}

void video_fec_parse_env(VideoFecConfig *cfg) {
    if (!cfg) return;
    cfg->enabled = 0;
    cfg->k = 3;
    cfg->timeout_ms = 50;

    const char *en = getenv("VIDEO_FEC");
    if (en && en[0] == '1' && en[1] == '\0') cfg->enabled = 1;

    const char *ks = getenv("VIDEO_FEC_K");
    if (ks) {
        int k = atoi(ks);
        if (k == 3 || k == 5) cfg->k = k;
    }

    const char *ts = getenv("VIDEO_FEC_TIMEOUT_MS");
    if (ts) {
        int t = atoi(ts);
        if (t < 50) t = 50;
        cfg->timeout_ms = t;
    }
}

static void xor_into(uint8_t *dst, int *dst_len, const uint8_t *src, int src_len) {
    if (src_len > *dst_len) {
        memset(dst + *dst_len, 0, (size_t)(src_len - *dst_len));
        *dst_len = src_len;
    }
    for (int i = 0; i < src_len; i++) dst[i] ^= src[i];
}

/* Insert one-byte HE after 12-byte fixed header (CC=0 assumed). */
static int insert_one_byte_he(const uint8_t *in,
                              int in_len,
                              uint16_t block_id,
                              uint8_t index,
                              uint8_t k_actual,
                              uint8_t *out,
                              int out_cap) {
    if (!in || !out || in_len < 12) return -1;
    if ((in[0] & 0x0F) != 0) return -1; /* CC must be 0 */
    if (in[0] & 0x10) return -1;         /* already has extension */
    if (in_len + VIDEO_FEC_HE_BYTES > out_cap) return -1;
    if (in_len + VIDEO_FEC_HE_BYTES > VIDEO_FEC_MAX_PKT) return -1;

    memcpy(out, in, 12);
    out[0] = (uint8_t)(out[0] | 0x10); /* X=1 */
    write_be16(out + 12, 0xBEDE);
    write_be16(out + 14, 2); /* 2 x 32-bit words */
    out[16] = (uint8_t)((VIDEO_FEC_EXT_ID << 4) | 4); /* 0xB4 */
    write_be16(out + 17, block_id);
    out[19] = index;
    out[20] = k_actual;
    out[21] = 0;
    out[22] = 0;
    out[23] = 0;
    memcpy(out + 24, in + 12, (size_t)(in_len - 12));
    return in_len + VIDEO_FEC_HE_BYTES;
}

static int strip_fec_he(const uint8_t *in, int in_len, uint8_t *out, int out_cap) {
    if (!in || !out || in_len < 12) return -1;
    if (!(in[0] & 0x10)) {
        if (in_len > out_cap) return -1;
        memcpy(out, in, (size_t)in_len);
        return in_len;
    }
    int cc = in[0] & 0x0F;
    int hl = 12 + cc * 4;
    if (in_len < hl + 4) {
        if (in_len > out_cap) return -1;
        memcpy(out, in, (size_t)in_len);
        return in_len;
    }
    uint16_t profile = read_be16(in + hl);
    uint16_t ext_words = read_be16(in + hl + 2);
    int ext_bytes = 4 + (int)ext_words * 4;
    if (profile != 0xBEDE || ext_words != 2 || in_len < hl + ext_bytes) {
        if (in_len > out_cap) return -1;
        memcpy(out, in, (size_t)in_len);
        return in_len;
    }
    if (in[hl + 4] != (uint8_t)((VIDEO_FEC_EXT_ID << 4) | 4)) {
        if (in_len > out_cap) return -1;
        memcpy(out, in, (size_t)in_len);
        return in_len;
    }
    int out_len = in_len - ext_bytes;
    if (out_len > out_cap) return -1;
    memcpy(out, in, (size_t)hl);
    out[0] = (uint8_t)(out[0] & (uint8_t)~0x10);
    memcpy(out + hl, in + hl + ext_bytes, (size_t)(in_len - hl - ext_bytes));
    return out_len;
}

typedef struct {
    uint16_t block_id;
    uint8_t index;
    uint8_t k_actual;
} FecHeInfo;

static int parse_fec_he(const uint8_t *pkt, int len, FecHeInfo *he) {
    if (!pkt || !he || len < 12) return -1;
    if (!(pkt[0] & 0x10)) return -1;
    int cc = pkt[0] & 0x0F;
    int hl = 12 + cc * 4;
    if (len < hl + 12) return -1;
    if (read_be16(pkt + hl) != 0xBEDE) return -1;
    if (read_be16(pkt + hl + 2) != 2) return -1;
    if (pkt[hl + 4] != (uint8_t)((VIDEO_FEC_EXT_ID << 4) | 4)) return -1;
    he->block_id = read_be16(pkt + hl + 5);
    he->index = pkt[hl + 7];
    he->k_actual = pkt[hl + 8];
    return 0;
}

static int build_fec_rtp(VideoFecSender *s, int k_actual, uint8_t *out, int out_cap) {
    if (k_actual < 1 || k_actual > s->k) return -1;
    int L = 0;
    for (int i = 0; i < k_actual; i++) {
        if ((int)s->lengths[i] > L) L = (int)s->lengths[i];
    }
    if (L > s->xor_len) L = s->xor_len;
    int need = 12 + 4 + 2 * k_actual + L;
    if (need > out_cap) return -1;

    out[0] = 0x80; /* V=2 */
    out[1] = (uint8_t)VIDEO_FEC_PT;
    write_be16(out + 2, s->fec_seq++);
    write_be32(out + 4, s->first_ts);
    write_be32(out + 8, s->fec_ssrc);
    out[12] = VIDEO_FEC_VERSION;
    out[13] = (uint8_t)k_actual;
    write_be16(out + 14, s->block_id);
    int off = 16;
    for (int i = 0; i < k_actual; i++) {
        write_be16(out + off, s->lengths[i]);
        off += 2;
    }
    memcpy(out + off, s->xor_buf, (size_t)L);
    return need;
}

static void sender_clear_block(VideoFecSender *s) {
    s->index = 0;
    s->xor_len = 0;
    memset(s->lengths, 0, sizeof(s->lengths));
    memset(s->xor_buf, 0, sizeof(s->xor_buf));
}

void video_fec_sender_init(VideoFecSender *s, const VideoFecConfig *cfg, uint32_t media_ssrc) {
    memset(s, 0, sizeof(*s));
    if (!cfg) return;
    s->enabled = cfg->enabled;
    s->k = cfg->k;
    s->media_ssrc = media_ssrc;
    s->fec_ssrc = video_fec_ssrc_from_media(media_ssrc);
    s->fec_seq = 1;
    s->block_id = 0;
}

int video_fec_sender_on_media(VideoFecSender *s,
                              const uint8_t *pkt,
                              int bytes,
                              uint32_t timestamp,
                              uint8_t *out_media,
                              int *out_media_len,
                              int out_cap,
                              uint8_t *out_fec,
                              int *out_fec_len,
                              int fec_cap) {
    if (out_fec_len) *out_fec_len = 0;
    if (!s || !pkt || !out_media || !out_media_len) return -1;
    if (!s->enabled) {
        if (bytes > out_cap) return -1;
        memcpy(out_media, pkt, (size_t)bytes);
        *out_media_len = bytes;
        return 0;
    }
    if (s->index < 0 || s->index >= s->k) return -1;
    int n = insert_one_byte_he(pkt, bytes, s->block_id, (uint8_t)s->index, (uint8_t)s->k, out_media, out_cap);
    if (n < 0) return -1;
    *out_media_len = n;

    if (s->index == 0) {
        s->first_ts = timestamp;
        memset(s->xor_buf, 0, sizeof(s->xor_buf));
        memcpy(s->xor_buf, out_media, (size_t)n);
        s->xor_len = n;
    } else {
        xor_into(s->xor_buf, &s->xor_len, out_media, n);
    }
    s->lengths[s->index] = (uint16_t)n;
    s->index++;

    if (s->index == s->k) {
        int fl = build_fec_rtp(s, s->k, out_fec, fec_cap);
        if (fl < 0) {
            /* Roll back so a later call is not permanently wedged at index==k. */
            s->index--;
            s->lengths[s->index] = 0;
            return -1;
        }
        if (out_fec_len) *out_fec_len = fl;
        s->fec_sent++;
        s->block_id++;
        sender_clear_block(s);
    }
    return 0;
}

int video_fec_sender_flush(VideoFecSender *s, uint8_t *out_fec, int *out_fec_len, int fec_cap) {
    if (out_fec_len) *out_fec_len = 0;
    if (!s || !s->enabled) return 0;
    if (s->index <= 0) return 0;
    int fl = build_fec_rtp(s, s->index, out_fec, fec_cap);
    if (fl < 0) return -1;
    if (out_fec_len) *out_fec_len = fl;
    s->fec_sent++;
    s->block_id++;
    sender_clear_block(s);
    return 0;
}

/* ---- receiver ---- */

static uint16_t expected_block_id(const VideoFecReceiver *r) {
    uint16_t min_open = 0;
    int have_open = 0;
    uint16_t base = r->has_last_closed ? (uint16_t)(r->last_closed + 1u) : 0;
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        if (!r->blocks[i].in_use) continue;
        if (!have_open) {
            min_open = r->blocks[i].block_id;
            have_open = 1;
        } else {
            uint16_t d_cur = (uint16_t)(r->blocks[i].block_id - base);
            uint16_t d_min = (uint16_t)(min_open - base);
            if (d_cur < d_min) min_open = r->blocks[i].block_id;
        }
    }
    if (have_open) return min_open;
    return base;
}

static int block_in_window(const VideoFecReceiver *r, uint16_t block_id) {
    uint16_t expected = expected_block_id(r);
    uint16_t start = (uint16_t)(expected - 1u);
    uint16_t d = (uint16_t)(block_id - start);
    return d <= (uint16_t)(VIDEO_FEC_MAX_FUTURE + 1);
}

static int effective_k(const VideoFecBlock *b) {
    if (b->has_received_fec) return b->fec_k;
    return b->k_from_media;
}

static VideoFecBlock *find_block(VideoFecReceiver *r, uint16_t block_id) {
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        if (r->blocks[i].in_use && r->blocks[i].block_id == block_id) return &r->blocks[i];
    }
    return NULL;
}

static int count_pending(const VideoFecReceiver *r) {
    int n = 0;
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        if (r->blocks[i].in_use) n++;
    }
    return n;
}

static int count_future(const VideoFecReceiver *r) {
    uint16_t expected = expected_block_id(r);
    int n = 0;
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        if (!r->blocks[i].in_use) continue;
        uint16_t d = (uint16_t)(r->blocks[i].block_id - expected);
        if (d > 0 && d <= VIDEO_FEC_MAX_FUTURE) n++;
    }
    return n;
}

static void free_block(VideoFecBlock *b) {
    memset(b, 0, sizeof(*b));
}

static void note_block_closed(VideoFecReceiver *r, VideoFecBlock *b) {
    r->last_closed = b->block_id;
    r->has_last_closed = 1;
}

static void record_wait(VideoFecReceiver *r, VideoFecBlock *b, int64_t now_us) {
    int64_t w = now_us - b->first_enqueue_us;
    if (w < 0) w = 0;
    r->stats.fec_wait_us_sum += w;
    r->stats.fec_closed_blocks++;
    if (w > r->stats.fec_wait_us_max) r->stats.fec_wait_us_max = w;
}

static int block_min_present_seq(const VideoFecBlock *b, uint16_t *out_seq) {
    int found = 0;
    uint16_t min_seq = 0;
    for (int i = 0; i < VIDEO_FEC_K_MAX; i++) {
        if (!b->slots[i].present) continue;
        if (!found || (int16_t)(b->slots[i].seq - min_seq) < 0) {
            min_seq = b->slots[i].seq;
            found = 1;
        }
    }
    if (found && out_seq) *out_seq = min_seq;
    return found;
}

static int block_max_present_seq(const VideoFecBlock *b, uint16_t *out_seq) {
    int found = 0;
    uint16_t max_seq = 0;
    for (int i = 0; i < VIDEO_FEC_K_MAX; i++) {
        if (!b->slots[i].present) continue;
        if (!found || (int16_t)(b->slots[i].seq - max_seq) > 0) {
            max_seq = b->slots[i].seq;
            found = 1;
        }
    }
    if (found && out_seq) *out_seq = max_seq;
    return found;
}

static void emit_one(VideoFecReceiver *r, const uint8_t *pkt_with_he, int len) {
    uint8_t raw[VIDEO_FEC_MAX_PKT];
    int n = strip_fec_he(pkt_with_he, len, raw, (int)sizeof(raw));
    if (n < 0) return;
    if (r->hooks.emit) r->hooks.emit(r->hooks.user, raw, n);
}

static void emit_sorted(VideoFecReceiver *r, VideoFecBlock *b) {
    int order[VIDEO_FEC_K_MAX];
    int n = 0;
    for (int i = 0; i < VIDEO_FEC_K_MAX; i++) {
        if (b->slots[i].present) order[n++] = i;
    }
    for (int i = 0; i < n; i++) {
        for (int j = i + 1; j < n; j++) {
            if ((int16_t)(b->slots[order[j]].seq - b->slots[order[i]].seq) < 0) {
                int t = order[i];
                order[i] = order[j];
                order[j] = t;
            }
        }
    }
    for (int i = 0; i < n; i++) {
        VideoFecSlot *sl = &b->slots[order[i]];
        emit_one(r, sl->data, sl->len);
    }
}

static int recover_one(VideoFecReceiver *r, VideoFecBlock *b) {
    int k = effective_k(b);
    if (k <= 0 || !b->has_received_fec) return -1;
    if (b->received_count != k - 1) return -1;

    int missing = -1;
    for (int i = 0; i < k; i++) {
        if (!b->slots[i].present) {
            if (missing >= 0) return -1;
            missing = i;
        }
    }
    if (missing < 0) return -1;

    int L = b->fec_xor_len;
    uint8_t rec[VIDEO_FEC_MAX_PKT];
    if (L > VIDEO_FEC_MAX_PKT) return -1;
    memcpy(rec, b->fec_xor, (size_t)L);
    for (int i = 0; i < k; i++) {
        if (i == missing) continue;
        const VideoFecSlot *sl = &b->slots[i];
        for (int j = 0; j < sl->len && j < L; j++) rec[j] ^= sl->data[j];
        /* short packets already zero-padded in sender XOR; recovered bytes beyond sl->len stay */
    }
    int out_len = (int)b->fec_lengths[missing];
    if (out_len <= 0 || out_len > L || out_len > VIDEO_FEC_MAX_PKT) return -1;

    VideoFecSlot *ms = &b->slots[missing];
    memcpy(ms->data, rec, (size_t)out_len);
    ms->len = out_len;
    ms->present = 1;
    ms->seq = read_be16(rec + 2);
    b->received_count++;
    if (!b->has_first_seq) {
        b->first_seq = (uint16_t)(ms->seq - (uint16_t)missing);
        b->has_first_seq = 1;
    }
    r->stats.fec_recovered++;
    return 0;
}

static void sync_seq_after_close(VideoFecReceiver *r, VideoFecBlock *b, int mode_hint_no_fec) {
    if (!r->hooks.last_rtp_seq || !r->hooks.rtp_seq_ready) return;

    uint16_t max_seq = 0;
    int have_max = block_max_present_seq(b, &max_seq);

    if (mode_hint_no_fec) {
        if (have_max) {
            *r->hooks.last_rtp_seq = max_seq;
            *r->hooks.rtp_seq_ready = 1;
        }
        return;
    }

    int k = effective_k(b);
    if (b->has_first_seq && k > 0) {
        *r->hooks.last_rtp_seq = (uint16_t)(b->first_seq + (uint16_t)(k - 1));
        *r->hooks.rtp_seq_ready = 1;
        return;
    }
    if (have_max) {
        *r->hooks.last_rtp_seq = max_seq;
        *r->hooks.rtp_seq_ready = 1;
    }
}

static void close_empty(VideoFecReceiver *r, VideoFecBlock *b, int64_t now_us) {
    (void)now_us;
    note_block_closed(r, b);
    free_block(b);
}

static void emit_partial_and_deep(VideoFecReceiver *r, VideoFecBlock *b, int64_t now_us, int hint_no_fec) {
    b->unrecoverable_closing = 1;
    emit_sorted(r, b);
    if (r->hooks.deep) r->hooks.deep(r->hooks.user, hint_no_fec ? "fec hint-close unrecoverable" : "fec block unrecoverable");
    sync_seq_after_close(r, b, hint_no_fec);
    record_wait(r, b, now_us);
    r->stats.fec_fail++;
    note_block_closed(r, b);
    free_block(b);
}

static int try_close(VideoFecReceiver *r, VideoFecBlock *b, int64_t now_us) {
    int k = effective_k(b);
    if (k > 0 && b->received_count >= k) {
        emit_sorted(r, b);
        /* successful close: seq advanced by emit/track */
        record_wait(r, b, now_us);
        note_block_closed(r, b);
        free_block(b);
        return 1;
    }
    if (k > 0 && b->has_received_fec && b->received_count == k - 1) {
        if (recover_one(r, b) == 0) {
            emit_sorted(r, b);
            record_wait(r, b, now_us);
            note_block_closed(r, b);
            free_block(b);
            return 1;
        }
    }
    return 0;
}

static void sort_blocks_by_d(VideoFecBlock **arr, int n, uint16_t expected) {
    for (int i = 1; i < n; i++) {
        VideoFecBlock *key = arr[i];
        uint16_t dk = (uint16_t)(key->block_id - expected);
        int j = i - 1;
        while (j >= 0) {
            uint16_t dj = (uint16_t)(arr[j]->block_id - expected);
            if (dj <= dk) break;
            arr[j + 1] = arr[j];
            j--;
        }
        arr[j + 1] = key;
    }
}

static void apply_newer_block_hint(VideoFecReceiver *r, uint16_t new_block_id, int64_t now_us) {
    uint16_t expected = expected_block_id(r);
    VideoFecBlock *to_close[VIDEO_FEC_MAX_PENDING];
    int n = 0;
    uint16_t d_new = (uint16_t)(new_block_id - expected);

    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        VideoFecBlock *b = &r->blocks[i];
        if (!b->in_use) continue;
        if (b->block_id == new_block_id) continue;
        uint16_t d_b = (uint16_t)(b->block_id - expected);
        if (d_b <= d_new) to_close[n++] = b;
    }
    sort_blocks_by_d(to_close, n, expected);
    for (int i = 0; i < n; i++) {
        VideoFecBlock *b = to_close[i];
        if (!b->in_use) continue;
        r->stats.fec_block_closed_by_new_block++;
        if (b->has_received_fec) {
            if (!try_close(r, b, now_us)) {
                emit_partial_and_deep(r, b, now_us, 0);
            }
        } else if (b->received_count == 0) {
            close_empty(r, b, now_us);
        } else {
            emit_partial_and_deep(r, b, now_us, 1);
        }
    }
}

static int seq_contiguous_with_output(const VideoFecReceiver *r, const VideoFecBlock *b) {
    if (!r->hooks.rtp_seq_ready || !*r->hooks.rtp_seq_ready) return 1;
    uint16_t min_seq = 0;
    if (!block_min_present_seq(b, &min_seq)) return 1;
    return min_seq == (uint16_t)(*r->hooks.last_rtp_seq + 1u);
}

static void evict_farthest_future(VideoFecReceiver *r, int64_t now_us) {
    uint16_t expected = expected_block_id(r);
    VideoFecBlock *far = NULL;
    uint16_t far_d = 0;
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        VideoFecBlock *b = &r->blocks[i];
        if (!b->in_use) continue;
        uint16_t d = (uint16_t)(b->block_id - expected);
        if (d == 0) continue;
        if (!far || d > far_d) {
            far = b;
            far_d = d;
        }
    }
    if (!far) return;
    if (far->received_count > 0 && seq_contiguous_with_output(r, far)) {
        emit_partial_and_deep(r, far, now_us, far->has_received_fec ? 0 : 1);
    } else {
        /* 不连续：丢弃已收包且不推进 last_closed（避免 expected 跳过中间块） */
        free_block(far);
    }
}

static VideoFecBlock *alloc_block(VideoFecReceiver *r, uint16_t block_id, int64_t now_us) {
    uint16_t expected = expected_block_id(r);
    uint16_t d_new = (uint16_t)(block_id - expected);
    int is_future = (d_new > 0 && d_new <= VIDEO_FEC_MAX_FUTURE);

    while (count_pending(r) >= VIDEO_FEC_MAX_PENDING ||
           (is_future && count_future(r) >= VIDEO_FEC_MAX_FUTURE)) {
        int before = count_pending(r);
        evict_farthest_future(r, now_us);
        if (count_pending(r) >= before) break;
    }

    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        if (!r->blocks[i].in_use) {
            VideoFecBlock *b = &r->blocks[i];
            memset(b, 0, sizeof(*b));
            b->in_use = 1;
            b->block_id = block_id;
            b->first_enqueue_us = now_us;
            return b;
        }
    }
    return NULL;
}

static VideoFecBlock *get_or_alloc_block(VideoFecReceiver *r, uint16_t block_id, int64_t now_us) {
    if (!block_in_window(r, block_id)) return NULL;
    VideoFecBlock *b = find_block(r, block_id);
    if (b) return b;
    /* In window but no pending slot: behind expected ⇒ late closed packet. */
    {
        uint16_t expected = expected_block_id(r);
        uint16_t d = (uint16_t)(block_id - expected);
        if (d > VIDEO_FEC_MAX_FUTURE) return NULL;
    }
    return alloc_block(r, block_id, now_us);
}

static void store_media(VideoFecReceiver *r, const uint8_t *pkt, int len, const FecHeInfo *he, int64_t now_us) {
    if (he->k_actual < 1 || he->k_actual > VIDEO_FEC_K_MAX) return;
    if (he->index >= he->k_actual) return;
    VideoFecBlock *b = get_or_alloc_block(r, he->block_id, now_us);
    if (!b) return;
    if (he->index >= VIDEO_FEC_K_MAX) return;

    int ek = effective_k(b);
    if (ek > 0 && he->k_actual != (uint8_t)ek) {
        r->stats.fec_conflict++;
        return;
    }
    if (ek == 0 && b->k_from_media > 0 && he->k_actual != (uint8_t)b->k_from_media) {
        r->stats.fec_conflict++;
        return;
    }

    VideoFecSlot *sl = &b->slots[he->index];
    if (len > VIDEO_FEC_MAX_PKT) return;
    if (!sl->present) b->received_count++;
    sl->present = 1;
    sl->len = len;
    sl->seq = read_be16(pkt + 2);
    memcpy(sl->data, pkt, (size_t)len);

    if (b->k_from_media == 0) b->k_from_media = he->k_actual;
    if (!b->has_first_seq) {
        b->first_seq = (uint16_t)(sl->seq - (uint16_t)he->index);
        b->has_first_seq = 1;
    }

    apply_newer_block_hint(r, he->block_id, now_us);
    b = find_block(r, he->block_id);
    if (b) try_close(r, b, now_us);
}

static void handle_fec(VideoFecReceiver *r, const uint8_t *pkt, int len, int64_t now_us) {
    if (len < 16) return;
    uint8_t version = pkt[12];
    if (version != VIDEO_FEC_VERSION) {
        r->stats.fec_recv_version_drop++;
        return;
    }
    int k_actual = pkt[13];
    uint16_t block_id = read_be16(pkt + 14);
    if (k_actual < 1 || k_actual > VIDEO_FEC_K_MAX) return;
    int hdr = 16 + 2 * k_actual;
    if (len < hdr) return;
    int xor_len = len - hdr;
    if (xor_len < 0 || xor_len > VIDEO_FEC_MAX_PKT) return;

    r->stats.fec_recv++;
    apply_newer_block_hint(r, block_id, now_us);

    VideoFecBlock *b = get_or_alloc_block(r, block_id, now_us);
    if (!b) return;

    b->has_received_fec = 1;
    b->fec_k = k_actual;
    for (int i = 0; i < k_actual; i++) b->fec_lengths[i] = read_be16(pkt + 16 + 2 * i);
    memcpy(b->fec_xor, pkt + hdr, (size_t)xor_len);
    b->fec_xor_len = xor_len;

    try_close(r, b, now_us);
}

void video_fec_receiver_init(VideoFecReceiver *r, const VideoFecConfig *cfg, const VideoFecRecvHooks *hooks) {
    memset(r, 0, sizeof(*r));
    if (cfg) {
        r->enabled = cfg->enabled;
        r->k = cfg->k;
        r->timeout_ms = cfg->timeout_ms;
    }
    if (hooks) r->hooks = *hooks;
}

void video_fec_receiver_input(VideoFecReceiver *r, const uint8_t *pkt, int len, int64_t now_us) {
    if (!r || !r->enabled || !pkt || len < 12) return;

    uint8_t pt = (uint8_t)(pkt[1] & 0x7F);
    uint32_t ssrc = read_be32(pkt + 8);

    if (!r->media_ssrc_known) {
        if (pt == VIDEO_FEC_PT) {
            r->fec_ssrc = ssrc;
            r->media_ssrc = ssrc ^ 0xFFFFFFFFu;
            if (r->media_ssrc == 0) r->media_ssrc = 1;
            r->media_ssrc_known = 1;
        } else if (pt == VIDEO_FEC_RTP_H265_PT) {
            r->media_ssrc = ssrc;
            r->fec_ssrc = video_fec_ssrc_from_media(ssrc);
            r->media_ssrc_known = 1;
        }
    }

    if (pt == VIDEO_FEC_PT || (r->media_ssrc_known && ssrc == r->fec_ssrc)) {
        handle_fec(r, pkt, len, now_us);
        return;
    }
    if (pt == VIDEO_FEC_RTP_H265_PT) {
        FecHeInfo he;
        if (parse_fec_he(pkt, len, &he) != 0) return;
        store_media(r, pkt, len, &he, now_us);
        return;
    }
    /* unknown PT: silent drop */
}

void video_fec_receiver_poll_timeouts(VideoFecReceiver *r, int64_t now_us) {
    if (!r || !r->enabled) return;
    uint16_t expected = expected_block_id(r);
    VideoFecBlock *to_close[VIDEO_FEC_MAX_PENDING];
    int n = 0;
    int64_t limit_us = (int64_t)r->timeout_ms * 1000;

    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        VideoFecBlock *b = &r->blocks[i];
        if (!b->in_use) continue;
        if (now_us - b->first_enqueue_us >= limit_us) to_close[n++] = b;
    }
    sort_blocks_by_d(to_close, n, expected);
    for (int i = 0; i < n; i++) {
        VideoFecBlock *b = to_close[i];
        if (!b->in_use) continue;
        if (b->received_count == 0) {
            close_empty(r, b, now_us);
        } else if (!try_close(r, b, now_us)) {
            emit_partial_and_deep(r, b, now_us, b->has_received_fec ? 0 : 1);
        }
    }
}

int video_fec_seq_recoverable(const VideoFecReceiver *r, uint16_t seq) {
    if (!r || !r->enabled) return 0;
    for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
        const VideoFecBlock *b = &r->blocks[i];
        if (!b->in_use || !b->has_first_seq || b->unrecoverable_closing) continue;
        int k = effective_k(b);
        if (k <= 0) k = r->k;
        if (k <= 0) continue;

        int missing = 0;
        int match_missing = 0;
        for (int j = 0; j < k; j++) {
            uint16_t s = (uint16_t)(b->first_seq + (uint16_t)j);
            if (!b->slots[j].present) {
                missing++;
                if (s == seq) match_missing = 1;
            } else if (b->slots[j].seq == seq) {
                /* Already buffered in pending: covered by FEC path, suppress deep. */
                return 1;
            }
        }
        /* XOR FEC 最多恢复 1 包；缺 ≥2 则不可恢复（无论是否已到 FEC） */
        if (match_missing && missing <= 1) return 1;
    }
    return 0;
}

void video_fec_receiver_reset_interval_stats(VideoFecReceiver *r) {
    if (!r) return;
    memset(&r->stats, 0, sizeof(r->stats));
}

VideoFecReceiver *video_fec_receiver_create(const VideoFecConfig *cfg, const VideoFecRecvHooks *hooks) {
    VideoFecReceiver *r = (VideoFecReceiver *)calloc(1, sizeof(*r));
    if (!r) return NULL;
    video_fec_receiver_init(r, cfg, hooks);
    return r;
}

void video_fec_receiver_destroy(VideoFecReceiver *r) {
    free(r);
}
