#define _DEFAULT_SOURCE
#include "platform_net.h"
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/time.h>
#include <signal.h>
#include <stdint.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <SDL2/SDL.h>
#include <SDL2/SDL_ttf.h>
#include <libswscale/swscale.h>

#include "fec.h"
#include "frame_ack.h"
#include "relay_proto.h"
#include "receiver_config.h"
#include "rtp_loss_recovery.h"
#include "rtp_reorder.h"
#include "rtp-payload.h"
#include "rtp-profile.h"
#include "scale_cover.h"

#if defined(_WIN32) || defined(__MINGW32__)
#include <process.h>
#endif

/* RelayHeader(~44) + FEC RTP(≤~1526) 仍远小于此上限 */
#define MAX_RTP_PACKET 2048
#define NALU_BUF_BYTES (256 * 1024)
#define KEYFRAME_REQ_INTERVAL_US 200000
#define UDP_RCVBUF_SIZE (2 * 1024 * 1024)
#define DECODE_FAIL_REQ_THRESHOLD 3
#define DAMAGED_TS_SLOTS 16
#define ARRIVAL_TS_SLOTS 256
#define OVERLAY_FONT_SIZE 18
#define PI_NET_RTT_STALE_US 2000000
#define PI_NET_RTT_AVG_WINDOW_US 5000000
#define PI_NET_RTT_SAMPLES 128
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

static volatile sig_atomic_t g_stop = 0;

#if defined(_WIN32) || defined(__MINGW32__)
static HANDLE g_parent_process = NULL;
static HANDLE g_session_mutex = NULL;

static int acquire_operator_guard(unsigned long parent_pid, const char *session_id) {
    if (parent_pid == 0 || !session_id || !*session_id) {
        fprintf(stderr, "--parent-pid requires a non-empty --session\n");
        return -1;
    }
    DWORD parent_session = 0, receiver_session = 0;
    if (!ProcessIdToSessionId((DWORD)parent_pid, &parent_session) ||
        !ProcessIdToSessionId(GetCurrentProcessId(), &receiver_session) ||
        parent_session != receiver_session) {
        fprintf(stderr, "receiver parent is not in this Windows session\n");
        return -1;
    }
    g_parent_process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)parent_pid);
    if (!g_parent_process) {
        fprintf(stderr, "receiver cannot open parent process %lu\n", parent_pid);
        return -1;
    }
    char mutex_name[RELAY_SESSION_ID_STR_SIZE + 32];
    snprintf(mutex_name, sizeof(mutex_name), "Local\\YAMReceiver_");
    size_t prefix = strlen(mutex_name);
    for (size_t i = 0; session_id[i] && prefix + i + 1 < sizeof(mutex_name); i++) {
        unsigned char c = (unsigned char)session_id[i];
        mutex_name[prefix + i] = (char)(isalnum(c) || c == '_' || c == '-' ? c : '_');
        mutex_name[prefix + i + 1] = '\0';
    }
    g_session_mutex = CreateMutexA(NULL, FALSE, mutex_name);
    if (!g_session_mutex || GetLastError() == ERROR_ALREADY_EXISTS) {
        fprintf(stderr, "receiver session already active: %s\n", session_id);
        if (g_session_mutex) CloseHandle(g_session_mutex);
        CloseHandle(g_parent_process);
        g_session_mutex = NULL;
        g_parent_process = NULL;
        return -1;
    }
    return 0;
}

static int operator_parent_alive(void) {
    if (!g_parent_process) return 1;
    DWORD wait = WaitForSingleObject(g_parent_process, 0);
    if (wait == WAIT_TIMEOUT) return 1;
    if (wait == WAIT_OBJECT_0) {
        fprintf(stderr, "receiver parent operator exited\n");
    } else {
        fprintf(stderr, "receiver parent check failed\n");
    }
    return 0;
}

static void release_operator_guard(void) {
    if (g_session_mutex) CloseHandle(g_session_mutex);
    if (g_parent_process) CloseHandle(g_parent_process);
    g_session_mutex = NULL;
    g_parent_process = NULL;
}
#else
static int acquire_operator_guard(unsigned long parent_pid, const char *session_id) {
    (void)parent_pid;
    (void)session_id;
    return 0;
}
static int operator_parent_alive(void) { return 1; }
static void release_operator_guard(void) {}
#endif

static void handle_sigint(int sig) {
    (void)sig;
    g_stop = 1;
}

typedef enum {
    TRANSPORT_MODE_DIRECT = 0,
    TRANSPORT_MODE_RELAY = 1,
} TransportMode;

typedef struct {
    AVCodecContext *dec_ctx;
    AVCodecParserContext *parser;
    AVFrame *frame;
    AVPacket *pkt;

    struct SwsContext *sws;
    AVFrame *yuv420;

    SDL_Window *window;
    SDL_Renderer *renderer;
    SDL_Texture *texture;

    int width;
    int height;

    int64_t last_report_us;
    int64_t rendered_interval;
    int64_t nalu_interval;
    int64_t rx_bytes_interval;
    int64_t vps_interval;
    int64_t sps_interval;
    int64_t pps_interval;
    int64_t idr_interval;
    int64_t cra_interval;
    int64_t decoded_interval;
    double decode_us_interval;
    int64_t media_packets_interval;
    int64_t media_packets_total;
    int64_t last_media_arrival_us;
    int64_t rtp_missing_packets_interval;
    int64_t rtp_missing_packets_total;
    int ctrl_fd;
    struct sockaddr_in ctrl_peer;
    TransportMode mode;
    char session_id[RELAY_SESSION_ID_STR_SIZE];
    int64_t last_heartbeat_us;
    uint32_t last_rtp_ts;
    int headless;
    int has_last_network_acked_frame;
    uint32_t last_network_acked_frame_id;
    int has_last_decode_acked_frame;
    uint32_t last_decode_acked_frame_id;
    int decode_ok;
    int64_t decode_fail_streak;
    int64_t last_keyframe_req_us;
    int64_t recovery_interval;
    int64_t decode_loss_interval;
    int64_t keyframe_req_interval;
    int64_t rtp_seq_gap_interval;
    int64_t damaged_nalu_drop_interval;
    RtpReorder reorder;
    int rtp_seq_ready;
    uint16_t last_rtp_seq;
    uint32_t last_rtp_packet_ts;
    int last_rtp_marker;
    uint32_t damaged_ts[DAMAGED_TS_SLOTS];
    int damaged_ts_valid[DAMAGED_TS_SLOTS];
    int damaged_ts_next;
    uint32_t arrival_ts[ARRIVAL_TS_SLOTS];
    int64_t arrival_us[ARRIVAL_TS_SLOTS];
    int arrival_valid[ARRIVAL_TS_SLOTS];
    int arrival_next;
    uint8_t *nalu_buf;
    int nalu_buf_cap;
    VideoFecReceiver *fec;
    void *rtp_unpacker;
    struct rtp_payload_t rtp_handler;
    RtpLossRecovery loss_recovery;
    TTF_Font *overlay_font;
    int overlay_enabled;
    int overlay_ttf_inited;
    double glass_ms_last;
    double glass_ms_avg;
    double overlay_fps;
    int64_t overlay_fps_last_us;
    int overlay_render_count;
    double pi_net_rtt_ms_last;
    double pi_net_rtt_ms_avg;
    int64_t pi_net_rtt_last_us;
    uint32_t pi_net_rtt_seq;
    double pi_net_rtt_samples[PI_NET_RTT_SAMPLES];
    int64_t pi_net_rtt_sample_us[PI_NET_RTT_SAMPLES];
    int pi_net_rtt_sample_next;
    const char *stream_name;
    int scale_cover;
    const char *health_file;
} ReceiverContext;

static int parse_transport_mode(const char *s, TransportMode *out) {
    if (!s || !out) return -1;
    if (strcmp(s, "direct") == 0) {
        *out = TRANSPORT_MODE_DIRECT;
        return 0;
    }
    if (strcmp(s, "relay") == 0) {
        *out = TRANSPORT_MODE_RELAY;
        return 0;
    }
    return -1;
}

static const char *transport_mode_name(TransportMode mode) {
    return mode == TRANSPORT_MODE_RELAY ? "relay" : "direct";
}

static int trim_path_to_parent(char *path) {
    char *forward = strrchr(path, '/');
    char *backward = strrchr(path, '\\');
    char *separator = forward;
    if (!separator || (backward && backward > separator)) separator = backward;
    if (!separator) return -1;
    *separator = '\0';
    return 0;
}

static int packaged_profiles_path(char *path, size_t path_size) {
    if (!path || path_size == 0) return -1;
#if defined(_WIN32) || defined(__MINGW32__)
    DWORD chars = GetModuleFileNameA(NULL, path, (DWORD)path_size);
    if (chars == 0 || chars >= path_size) return -1;
    path[chars] = '\0';
#else
    ssize_t chars = readlink("/proc/self/exe", path, path_size - 1);
    if (chars <= 0 || (size_t)chars >= path_size) return -1;
    path[chars] = '\0';
#endif
    /* receiver.exe -> receiver -> runtime -> portable bundle root. */
    if (trim_path_to_parent(path) != 0 || trim_path_to_parent(path) != 0 ||
        trim_path_to_parent(path) != 0) {
        return -1;
    }
    const char *suffix = "/config/profiles.yaml";
    size_t length = strlen(path);
    if (length + strlen(suffix) >= path_size) return -1;
    memcpy(path + length, suffix, strlen(suffix) + 1);
    return 0;
}

static void send_frame_ack(ReceiverContext *ctx, uint32_t frame_id, uint32_t kind);
static int send_keyframe_req(ReceiverContext *ctx);
static void send_heartbeat(ReceiverContext *ctx);
static void enter_decode_loss(ReceiverContext *ctx, const char *reason, int deep);

static int h265_nalu_type_from_annexb(const uint8_t *data, int bytes) {
    // Expect a 4-byte start code (00 00 00 01) prepended by rtp_on_nalu.
    if (!data || bytes < 6) return -1;
    return (data[4] >> 1) & 0x3F;
}

static int h265_nal_is_recovery_point(int nal_type) {
    return nal_type == 32 || nal_type == 33 || nal_type == 34 || nal_type == 19 || nal_type == 20 ||
           nal_type == 21;
}

static int h265_nal_is_idr(int nal_type) {
    return nal_type == 19 || nal_type == 20;
}

static void reset_hevc_parser(ReceiverContext *ctx) {
    if (!ctx->parser) return;
    av_parser_close(ctx->parser);
    ctx->parser = av_parser_init(AV_CODEC_ID_HEVC);
}

static void mark_damaged_timestamp(ReceiverContext *ctx, uint32_t timestamp) {
    for (int i = 0; i < DAMAGED_TS_SLOTS; i++) {
        if (ctx->damaged_ts_valid[i] && ctx->damaged_ts[i] == timestamp) return;
    }
    int slot = ctx->damaged_ts_next++ % DAMAGED_TS_SLOTS;
    ctx->damaged_ts[slot] = timestamp;
    ctx->damaged_ts_valid[slot] = 1;
}

static int is_damaged_timestamp(ReceiverContext *ctx, uint32_t timestamp) {
    for (int i = 0; i < DAMAGED_TS_SLOTS; i++) {
        if (ctx->damaged_ts_valid[i] && ctx->damaged_ts[i] == timestamp) return 1;
    }
    return 0;
}

static void note_frame_arrival(ReceiverContext *ctx, uint32_t rtp_ts, int64_t now_us) {
    for (int i = 0; i < ARRIVAL_TS_SLOTS; i++) {
        if (ctx->arrival_valid[i] && ctx->arrival_ts[i] == rtp_ts) return;
    }
    int slot = ctx->arrival_next++ % ARRIVAL_TS_SLOTS;
    ctx->arrival_ts[slot] = rtp_ts;
    ctx->arrival_us[slot] = now_us;
    ctx->arrival_valid[slot] = 1;
}

static int64_t lookup_frame_arrival(const ReceiverContext *ctx, uint32_t rtp_ts) {
    for (int i = 0; i < ARRIVAL_TS_SLOTS; i++) {
        if (ctx->arrival_valid[i] && ctx->arrival_ts[i] == rtp_ts) return ctx->arrival_us[i];
    }
    return 0;
}

static uint16_t read_be16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t read_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static int parse_rtp_packet_header(const uint8_t *packet,
                                   ssize_t bytes,
                                   uint16_t *seq,
                                   uint32_t *timestamp,
                                   int *marker) {
    if (!packet || bytes < 12) return -1;
    if ((packet[0] >> 6) != 2) return -1;
    int cc = packet[0] & 0x0F;
    int header_bytes = 12 + cc * 4;
    if (bytes < header_bytes) return -1;
    *marker = (packet[1] & 0x80) != 0;
    *seq = read_be16(packet + 2);
    *timestamp = read_be32(packet + 4);
    return 0;
}

static void note_media_packet_arrival(ReceiverContext *ctx, const uint8_t *payload, ssize_t payload_len, int64_t now_us) {
    if (!payload || payload_len < 12) return;
    uint8_t pt = (uint8_t)(payload[1] & 0x7F);
    if (pt != VIDEO_FEC_RTP_H265_PT) return;
    uint16_t seq = 0;
    uint32_t timestamp = 0;
    int marker = 0;
    if (parse_rtp_packet_header(payload, payload_len, &seq, &timestamp, &marker) != 0) return;
    ctx->media_packets_interval++;
    ctx->media_packets_total++;
    ctx->last_media_arrival_us = now_us;
    note_frame_arrival(ctx, timestamp, now_us);
}

static void ordered_rtp_emit(void *user, const uint8_t *pkt, int len) {
    ReceiverContext *ctx = (ReceiverContext *)user;
    uint16_t seq = 0;
    uint32_t timestamp = 0;
    int marker = 0;
    if (parse_rtp_packet_header(pkt, len, &seq, &timestamp, &marker) != 0) return;
    ctx->last_rtp_seq = seq;
    ctx->last_rtp_packet_ts = timestamp;
    ctx->last_rtp_marker = marker;
    ctx->rtp_seq_ready = 1;
    if (ctx->rtp_unpacker) rtp_payload_decode_input(ctx->rtp_unpacker, pkt, len);
}

static void ordered_rtp_missing(void *user, uint16_t first_missing, uint16_t missing_count) {
    ReceiverContext *ctx = (ReceiverContext *)user;
    ctx->rtp_seq_gap_interval++;
    ctx->rtp_missing_packets_interval += missing_count;
    ctx->rtp_missing_packets_total += missing_count;
    rtp_loss_recovery_note_missing(&ctx->loss_recovery, first_missing, missing_count);
    ctx->decode_ok = ctx->loss_recovery.decode_ok;
}

static void fec_emit_cb(void *user, const uint8_t *pkt, int len) {
    ReceiverContext *ctx = (ReceiverContext *)user;
    uint16_t seq = 0;
    uint32_t timestamp = 0;
    int marker = 0;
    if (parse_rtp_packet_header(pkt, len, &seq, &timestamp, &marker) != 0) return;
    ctx->last_rtp_seq = seq;
    ctx->last_rtp_packet_ts = timestamp;
    ctx->last_rtp_marker = marker;
    ctx->rtp_seq_ready = 1;
    if (ctx->rtp_unpacker) rtp_payload_decode_input(ctx->rtp_unpacker, pkt, len);
}

static void fec_deep_cb(void *user, const char *reason) {
    enter_decode_loss((ReceiverContext *)user, reason, 1);
}

static int send_keyframe_req(ReceiverContext *ctx) {
    if (ctx->ctrl_fd < 0) return 0;
    int64_t now = av_gettime_relative();
    if (now - ctx->last_keyframe_req_us < KEYFRAME_REQ_INTERVAL_US) return 0;
    ctx->last_keyframe_req_us = now;
    FrameAck req;
    req.magic = FRAME_ACK_MAGIC;
    req.kind = FRAME_ACK_KIND_KEYFRAME_REQ;
    req.frame_id = 0;
    ssize_t sent = 0;
    if (ctx->mode == TRANSPORT_MODE_RELAY) {
        uint8_t buf[RELAY_HEADER_SIZE + sizeof(req)];
        relay_header_write(buf, RELAY_TYPE_HEARTBEAT, ctx->session_id);
        memcpy(buf + RELAY_HEADER_SIZE, &req, sizeof(req));
        sent = sendto(ctx->ctrl_fd, buf, sizeof(buf), 0, (const struct sockaddr *)&ctx->ctrl_peer, sizeof(ctx->ctrl_peer));
        if (sent == (ssize_t)sizeof(buf)) {
            ctx->keyframe_req_interval++;
            return 1;
        }
        return 0;
    }
    sent = sendto(ctx->ctrl_fd, &req, sizeof(req), 0, (const struct sockaddr *)&ctx->ctrl_peer, sizeof(ctx->ctrl_peer));
    if (sent == (ssize_t)sizeof(req)) {
        ctx->keyframe_req_interval++;
        return 1;
    }
    return 0;
}

static void recreate_rtp_unpacker(ReceiverContext *ctx) {
    if (ctx->rtp_unpacker) {
        rtp_payload_decode_destroy(ctx->rtp_unpacker);
        ctx->rtp_unpacker = NULL;
    }
    ctx->rtp_unpacker = rtp_payload_decode_create(RTP_PAYLOAD_H265, "H265", &ctx->rtp_handler, ctx);
    if (!ctx->rtp_unpacker) {
        fprintf(stderr, "[receiver] rtp_payload_decode_create failed during recovery\n");
    }
}

static void apply_pending_rtp_loss(ReceiverContext *ctx) {
    uint16_t first = 0;
    uint16_t count = 0;
    if (!rtp_loss_recovery_take(&ctx->loss_recovery, &first, &count)) return;
    ctx->decode_ok = 0;
    fprintf(stderr, "[receiver] rtp missing: first=%u count=%u\n", first, count);
    enter_decode_loss(ctx, "rtp missing", 1);
}

static void enter_decode_loss(ReceiverContext *ctx, const char *reason, int deep) {
    if (reason) fprintf(stderr, "[receiver] decode loss: %s\n", reason);
    ctx->decode_loss_interval++;
    if (deep) {
        ctx->recovery_interval++;
        if (ctx->dec_ctx) avcodec_flush_buffers(ctx->dec_ctx);
        reset_hevc_parser(ctx);
        rtp_reorder_flush(&ctx->reorder);
        recreate_rtp_unpacker(ctx);
        ctx->decode_ok = 0;
        ctx->loss_recovery.decode_ok = 0;
        (void)rtp_loss_recovery_take(&ctx->loss_recovery, NULL, NULL);
        send_keyframe_req(ctx);
    }
    ctx->decode_fail_streak++;
    if (!deep && ctx->decode_fail_streak >= DECODE_FAIL_REQ_THRESHOLD) {
        send_keyframe_req(ctx);
        ctx->decode_fail_streak = 0;
    }
}

static int ensure_sdl_video(ReceiverContext *ctx, AVFrame *decoded) {
    if (ctx->width == decoded->width && ctx->height == decoded->height && ctx->texture && ctx->sws) {
        return 0;
    }

    ctx->width = decoded->width;
    ctx->height = decoded->height;

    if (ctx->texture) {
        SDL_DestroyTexture(ctx->texture);
        ctx->texture = NULL;
    }
    if (ctx->sws) {
        sws_freeContext(ctx->sws);
        ctx->sws = NULL;
    }
    if (ctx->yuv420) {
        av_frame_free(&ctx->yuv420);
    }

    ctx->texture = SDL_CreateTexture(ctx->renderer,
                                     SDL_PIXELFORMAT_IYUV,
                                     SDL_TEXTUREACCESS_STREAMING,
                                     ctx->width,
                                     ctx->height);
    if (!ctx->texture) {
        fprintf(stderr, "SDL_CreateTexture failed: %s\n", SDL_GetError());
        return -1;
    }

    ctx->yuv420 = av_frame_alloc();
    if (!ctx->yuv420) return -1;
    ctx->yuv420->format = AV_PIX_FMT_YUV420P;
    ctx->yuv420->width = ctx->width;
    ctx->yuv420->height = ctx->height;
    if (av_frame_get_buffer(ctx->yuv420, 32) < 0) return -1;

    ctx->sws = sws_getContext(decoded->width,
                              decoded->height,
                              (enum AVPixelFormat)decoded->format,
                              decoded->width,
                              decoded->height,
                              AV_PIX_FMT_YUV420P,
                              SWS_BILINEAR,
                              NULL,
                              NULL,
                              NULL);
    if (!ctx->sws) return -1;

    SDL_SetWindowSize(ctx->window, ctx->width, ctx->height);
    return 0;
}

static int get_exe_dir(char *buf, size_t cap) {
    if (!buf || cap == 0) return -1;
#if defined(_WIN32) || defined(__MINGW32__)
    DWORD n = GetModuleFileNameA(NULL, buf, (DWORD)cap);
    if (n == 0 || n >= cap) return -1;
    char *slash = strrchr(buf, '\\');
    if (!slash) slash = strrchr(buf, '/');
    if (!slash) return -1;
    *slash = '\0';
    return 0;
#else
    ssize_t n = readlink("/proc/self/exe", buf, cap - 1);
    if (n <= 0) return -1;
    buf[n] = '\0';
    char *slash = strrchr(buf, '/');
    if (!slash) return -1;
    *slash = '\0';
    return 0;
#endif
}

static int try_fc_match_font(char *out, size_t out_cap) {
    FILE *fp = popen("fc-match -f '%{file}\n' 'DejaVu Sans' 2>/dev/null", "r");
    if (!fp) return -1;
    if (!fgets(out, (int)out_cap, fp)) {
        pclose(fp);
        return -1;
    }
    pclose(fp);
    size_t len = strlen(out);
    while (len > 0 && (out[len - 1] == '\n' || out[len - 1] == '\r')) {
        out[--len] = '\0';
    }
    if (len == 0 || access(out, R_OK) != 0) return -1;
    return 0;
}

static int try_open_overlay_font(TTF_Font **font, const char *path) {
    if (!font || !path || !path[0]) return -1;
    if (access(path, R_OK) != 0) return -1;
    *font = TTF_OpenFont(path, OVERLAY_FONT_SIZE);
    return (*font != NULL) ? 0 : -1;
}

static int init_overlay(ReceiverContext *ctx) {
    if (ctx->headless || !ctx->overlay_enabled) return 0;
    if (TTF_Init() != 0) {
        fprintf(stderr, "warning: TTF_Init failed: %s (overlay disabled)\n", TTF_GetError());
        ctx->overlay_enabled = 0;
        return 0;
    }
    ctx->overlay_ttf_inited = 1;

    const char *env_font = getenv("OVERLAY_FONT");
    char fc_path[PATH_MAX];
    char bundled_path[PATH_MAX];
    char exe_dir[PATH_MAX];
    const char *candidates[20];
    int n_candidates = 0;

    if (env_font && env_font[0]) candidates[n_candidates++] = env_font;
    if (get_exe_dir(exe_dir, sizeof(exe_dir)) == 0) {
        int need = snprintf(bundled_path, sizeof(bundled_path), "%s/fonts/DejaVuSans.ttf", exe_dir);
        if (need > 0 && (size_t)need < sizeof(bundled_path)) {
            candidates[n_candidates++] = bundled_path;
        }
    }
    if (try_fc_match_font(fc_path, sizeof(fc_path)) == 0) {
        candidates[n_candidates++] = fc_path;
    }

    static const char *system_fonts[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        NULL,
    };
    for (int i = 0; system_fonts[i] && n_candidates < (int)(sizeof(candidates) / sizeof(candidates[0])); i++) {
        candidates[n_candidates++] = system_fonts[i];
    }

    for (int i = 0; i < n_candidates; i++) {
        if (try_open_overlay_font(&ctx->overlay_font, candidates[i]) == 0) {
            fprintf(stderr, "overlay font: %s\n", candidates[i]);
            fflush(stderr);
            return 0;
        }
    }

    fprintf(stderr,
            "warning: overlay font not found (overlay disabled); install fonts then rebuild:\n"
            "  sudo apt install fonts-dejavu-core fontconfig\n"
            "  ./scripts/build-vm-receiver.sh\n");
    ctx->overlay_enabled = 0;
    return 0;
}

static void destroy_overlay(ReceiverContext *ctx) {
    if (ctx->overlay_font) {
        TTF_CloseFont(ctx->overlay_font);
        ctx->overlay_font = NULL;
    }
    if (ctx->overlay_ttf_inited) {
        TTF_Quit();
        ctx->overlay_ttf_inited = 0;
    }
}

static void apply_pi_net_stats(ReceiverContext *ctx, const FrameAckStats *stats_pkt, int64_t now) {
    if (!ctx || !stats_pkt || !frame_ack_stats_valid(stats_pkt)) return;
    if (ctx->pi_net_rtt_last_us != 0 &&
        stats_pkt->seq - ctx->pi_net_rtt_seq >= 0x80000000u) {
        return;
    }
    ctx->pi_net_rtt_seq = stats_pkt->seq;
    ctx->pi_net_rtt_ms_last = frame_ack_rtt_x10_to_ms(stats_pkt->net_rtt_last_x10);
    ctx->pi_net_rtt_samples[ctx->pi_net_rtt_sample_next] = ctx->pi_net_rtt_ms_last;
    ctx->pi_net_rtt_sample_us[ctx->pi_net_rtt_sample_next] = now;
    ctx->pi_net_rtt_sample_next = (ctx->pi_net_rtt_sample_next + 1) % PI_NET_RTT_SAMPLES;

    double sum_ms = 0.0;
    int sample_count = 0;
    for (int i = 0; i < PI_NET_RTT_SAMPLES; i++) {
        if (now - ctx->pi_net_rtt_sample_us[i] <= PI_NET_RTT_AVG_WINDOW_US) {
            sum_ms += ctx->pi_net_rtt_samples[i];
            sample_count++;
        }
    }
    ctx->pi_net_rtt_ms_avg = sample_count > 0 ? sum_ms / (double)sample_count : 0.0;
    ctx->pi_net_rtt_last_us = now;
}

static void draw_overlay(ReceiverContext *ctx) {
    if (!ctx->overlay_enabled || !ctx->overlay_font || !ctx->renderer) return;

    char line[128];
    int64_t now = av_gettime_relative();
    int pi_rtt_fresh =
        ctx->pi_net_rtt_last_us > 0 && (now - ctx->pi_net_rtt_last_us) <= PI_NET_RTT_STALE_US;
    if (ctx->mode == TRANSPORT_MODE_RELAY && pi_rtt_fresh) {
        snprintf(line,
                 sizeof(line),
                 "RTT  %.1f ms   %.0f fps",
                 ctx->pi_net_rtt_ms_avg,
                 ctx->overlay_fps);
    } else if (ctx->mode == TRANSPORT_MODE_RELAY && ctx->pi_net_rtt_last_us > 0) {
        snprintf(line, sizeof(line), "RTT  stale   %.0f fps", ctx->overlay_fps);
    } else {
        snprintf(line, sizeof(line), "RTT  n/a   %.0f fps", ctx->overlay_fps);
    }

    SDL_Color white = {255, 255, 255, 255};
    SDL_Surface *s1 = TTF_RenderText_Blended(ctx->overlay_font, line, white);
    if (!s1) return;

    SDL_Rect bg = {4, 4, s1->w + 8, s1->h + 6};
    SDL_SetRenderDrawBlendMode(ctx->renderer, SDL_BLENDMODE_BLEND);
    SDL_SetRenderDrawColor(ctx->renderer, 0, 0, 0, 160);
    SDL_RenderFillRect(ctx->renderer, &bg);

    SDL_Texture *t1 = SDL_CreateTextureFromSurface(ctx->renderer, s1);
    if (t1) {
        SDL_Rect dst1 = {8, 7, s1->w, s1->h};
        SDL_RenderCopy(ctx->renderer, t1, NULL, &dst1);
        SDL_DestroyTexture(t1);
    }
    SDL_FreeSurface(s1);
}

static SDL_Rect fit_aspect_dst(int out_w, int out_h, int tex_w, int tex_h) {
    int box_w = out_w > 0 ? out_w : tex_w;
    int box_h = out_h > 0 ? out_h : tex_h;
    SDL_Rect dst = {0, 0, box_w, box_h};
    if (tex_w <= 0 || tex_h <= 0 || box_w <= 0 || box_h <= 0) {
        return dst;
    }
    double src_aspect = (double)tex_w / (double)tex_h;
    double dst_aspect = (double)box_w / (double)box_h;
    if (dst_aspect > src_aspect) {
        dst.h = box_h;
        dst.w = (int)((double)box_h * src_aspect);
        if (dst.w < 1) dst.w = 1;
        dst.x = (box_w - dst.w) / 2;
        dst.y = 0;
    } else {
        dst.w = box_w;
        dst.h = (int)((double)box_w / src_aspect);
        if (dst.h < 1) dst.h = 1;
        dst.x = 0;
        dst.y = (box_h - dst.h) / 2;
    }
    return dst;
}

static int render_frame(ReceiverContext *ctx, AVFrame *decoded) {
    if (ctx->headless) return 0;
    if (ensure_sdl_video(ctx, decoded) != 0) return -1;

    if (av_frame_make_writable(ctx->yuv420) < 0) return -1;

    sws_scale(ctx->sws,
              (const uint8_t *const *)decoded->data,
              decoded->linesize,
              0,
              decoded->height,
              ctx->yuv420->data,
              ctx->yuv420->linesize);

    SDL_UpdateYUVTexture(ctx->texture,
                         NULL,
                         ctx->yuv420->data[0], ctx->yuv420->linesize[0],
                         ctx->yuv420->data[1], ctx->yuv420->linesize[1],
                         ctx->yuv420->data[2], ctx->yuv420->linesize[2]);

    SDL_RenderClear(ctx->renderer);
    int out_w = 0;
    int out_h = 0;
    SDL_GetRendererOutputSize(ctx->renderer, &out_w, &out_h);
    if (ctx->scale_cover) {
        ScaleCoverRect crop = scale_cover_src(ctx->width, ctx->height, out_w, out_h);
        SDL_Rect src = {crop.x, crop.y, crop.w, crop.h};
        SDL_Rect dst = {0, 0, out_w, out_h};
        SDL_RenderCopy(ctx->renderer, ctx->texture, &src, &dst);
    } else {
        SDL_Rect dst = fit_aspect_dst(out_w, out_h, ctx->width, ctx->height);
        SDL_RenderCopy(ctx->renderer, ctx->texture, NULL, &dst);
    }

    int64_t t_render = av_gettime_relative();
    int64_t t0 = lookup_frame_arrival(ctx, ctx->last_rtp_ts);
    if (t0 > 0 && t_render > t0) {
        ctx->glass_ms_last = (double)(t_render - t0) / 1000.0;
        if (ctx->glass_ms_avg <= 0.0) {
            ctx->glass_ms_avg = ctx->glass_ms_last;
        } else {
            ctx->glass_ms_avg = ctx->glass_ms_avg * 0.85 + ctx->glass_ms_last * 0.15;
        }
    }

    ctx->overlay_render_count++;
    if (ctx->overlay_fps_last_us <= 0) {
        ctx->overlay_fps_last_us = t_render;
    } else if (t_render - ctx->overlay_fps_last_us >= 1000000) {
        ctx->overlay_fps =
            (double)ctx->overlay_render_count * 1000000.0 / (double)(t_render - ctx->overlay_fps_last_us);
        ctx->overlay_render_count = 0;
        ctx->overlay_fps_last_us = t_render;
    }

    draw_overlay(ctx);
    SDL_RenderPresent(ctx->renderer);
    return 0;
}

static int frame_is_corrupt(AVFrame *frame) {
#if defined(AV_FRAME_FLAG_CORRUPT)
    return (frame->flags & AV_FRAME_FLAG_CORRUPT) != 0;
#else
    (void)frame;
    return 0;
#endif
}

static int decode_and_render(ReceiverContext *ctx, const uint8_t *data, int bytes) {
    int had_error = 0;
    while (bytes > 0) {
        int used = av_parser_parse2(ctx->parser,
                                    ctx->dec_ctx,
                                    &ctx->pkt->data,
                                    &ctx->pkt->size,
                                    data,
                                    bytes,
                                    AV_NOPTS_VALUE,
                                    AV_NOPTS_VALUE,
                                    0);
        if (used < 0) {
            fprintf(stderr, "av_parser_parse2 failed\n");
            enter_decode_loss(ctx, "parser failed", 1);
            return -1;
        }

        data += used;
        bytes -= used;

        if (ctx->pkt->size <= 0) continue;

        int64_t t0 = av_gettime_relative();
        int ret = avcodec_send_packet(ctx->dec_ctx, ctx->pkt);
        if (ret < 0) {
            enter_decode_loss(ctx, "send_packet failed", 1);
            had_error = 1;
            continue;
        }

        int decoded_count = 0;
        while (ret >= 0) {
            ret = avcodec_receive_frame(ctx->dec_ctx, ctx->frame);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
            if (ret < 0) {
                enter_decode_loss(ctx, "receive_frame failed", 1);
                had_error = 1;
                break;
            }

            if (frame_is_corrupt(ctx->frame)) {
                enter_decode_loss(ctx, "corrupt frame", 1);
                av_frame_unref(ctx->frame);
                had_error = 1;
                continue;
            }

            if (!ctx->decode_ok) {
                av_frame_unref(ctx->frame);
                continue;
            }

            if (render_frame(ctx, ctx->frame) != 0) return -1;
            ctx->rendered_interval++;
            ctx->decoded_interval++;
            decoded_count++;
            ctx->decode_fail_streak = 0;
            send_frame_ack(ctx, ctx->last_rtp_ts, FRAME_ACK_KIND_DECODE);
            av_frame_unref(ctx->frame);
        }
        if (decoded_count > 0) {
            int64_t t1 = av_gettime_relative();
            ctx->decode_us_interval += (double)(t1 - t0);
        }
    }
    return had_error ? -1 : 0;
}

static void tune_udp_recv_socket(int fd) {
    int bufsize = UDP_RCVBUF_SIZE;
    if (setsockopt(fd, SOL_SOCKET, SO_RCVBUF, (const char *)&bufsize, sizeof(bufsize)) < 0) {
        perror("warning: SO_RCVBUF");
    }
}

static int open_ctrl_socket(void) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    (void)socket_set_nonblock(fd);
    return fd;
}

static void send_frame_ack(ReceiverContext *ctx, uint32_t frame_id, uint32_t kind) {
    if (ctx->ctrl_fd < 0) return;
    if (kind == FRAME_ACK_KIND_NETWORK) {
        if (ctx->has_last_network_acked_frame && ctx->last_network_acked_frame_id == frame_id) return;
    } else if (kind == FRAME_ACK_KIND_DECODE) {
        if (ctx->has_last_decode_acked_frame && ctx->last_decode_acked_frame_id == frame_id) return;
    } else {
        return;
    }
    FrameAck ack;
    ack.magic = FRAME_ACK_MAGIC;
    ack.kind = kind;
    ack.frame_id = frame_id;
    if (ctx->mode == TRANSPORT_MODE_RELAY) {
        uint8_t buf[RELAY_HEADER_SIZE + sizeof(ack)];
        relay_header_write(buf, RELAY_TYPE_HEARTBEAT, ctx->session_id);
        memcpy(buf + RELAY_HEADER_SIZE, &ack, sizeof(ack));
        (void)sendto(ctx->ctrl_fd, buf, sizeof(buf), 0, (const struct sockaddr *)&ctx->ctrl_peer, sizeof(ctx->ctrl_peer));
    } else {
        (void)sendto(ctx->ctrl_fd, &ack, sizeof(ack), 0, (const struct sockaddr *)&ctx->ctrl_peer, sizeof(ctx->ctrl_peer));
    }
    if (kind == FRAME_ACK_KIND_NETWORK) {
        ctx->has_last_network_acked_frame = 1;
        ctx->last_network_acked_frame_id = frame_id;
    } else {
        ctx->has_last_decode_acked_frame = 1;
        ctx->last_decode_acked_frame_id = frame_id;
    }
}

static void send_heartbeat(ReceiverContext *ctx) {
    if (ctx->ctrl_fd < 0 || ctx->mode != TRANSPORT_MODE_RELAY) return;
    uint8_t buf[RELAY_HEADER_SIZE];
    relay_header_write(buf, RELAY_TYPE_HEARTBEAT, ctx->session_id);
    (void)sendto(ctx->ctrl_fd, buf, sizeof(buf), 0, (const struct sockaddr *)&ctx->ctrl_peer, sizeof(ctx->ctrl_peer));
}

static void *rtp_alloc(void *param, int bytes) {
    (void)param;
    return malloc((size_t)bytes + 4);
}

static void rtp_free(void *param, void *packet) {
    (void)param;
    free(packet);
}

static int rtp_on_nalu(void *param, const void *packet, int bytes, uint32_t timestamp, int flags) {
    (void)flags;
    ReceiverContext *ctx = (ReceiverContext *)param;
    if (bytes + 4 > ctx->nalu_buf_cap) {
        fprintf(stderr, "[receiver] nalu too large: %d bytes\n", bytes);
        return -1;
    }
    ctx->nalu_buf[0] = 0;
    ctx->nalu_buf[1] = 0;
    ctx->nalu_buf[2] = 0;
    ctx->nalu_buf[3] = 1;
    memcpy(ctx->nalu_buf + 4, packet, (size_t)bytes);
    ctx->nalu_interval++;
    ctx->last_rtp_ts = timestamp;
    int nal_type = h265_nalu_type_from_annexb(ctx->nalu_buf, bytes + 4);
    if (nal_type == 32) ctx->vps_interval++;
    if (nal_type == 33) ctx->sps_interval++;
    if (nal_type == 34) ctx->pps_interval++;
    if (nal_type == 19 || nal_type == 20) ctx->idr_interval++;
    if (nal_type == 21) ctx->cra_interval++;

    if (is_damaged_timestamp(ctx, timestamp)) {
        ctx->damaged_nalu_drop_interval++;
        send_frame_ack(ctx, timestamp, FRAME_ACK_KIND_NETWORK);
        return 0;
    }

    if (!ctx->decode_ok && !h265_nal_is_recovery_point(nal_type)) {
        send_frame_ack(ctx, timestamp, FRAME_ACK_KIND_NETWORK);
        return 0;
    }

    if (h265_nal_is_idr(nal_type) || nal_type == 21) {
        ctx->decode_ok = 1;
        ctx->loss_recovery.decode_ok = 1;
        ctx->decode_fail_streak = 0;
    }

    send_frame_ack(ctx, timestamp, FRAME_ACK_KIND_NETWORK);
    if (decode_and_render(ctx, ctx->nalu_buf, bytes + 4) < 0 && !ctx->decode_ok) {
        send_keyframe_req(ctx);
    }
    return 0;
}

static long receiver_pid(void) {
#if defined(_WIN32) || defined(__MINGW32__)
    return (long)_getpid();
#else
    return (long)getpid();
#endif
}

static int replace_health_file(const char *tmp_path, const char *path) {
#if defined(_WIN32) || defined(__MINGW32__)
    return MoveFileExA(tmp_path, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 0 : -1;
#else
    return rename(tmp_path, path);
#endif
}

static void write_receiver_health(ReceiverContext *ctx,
                                  int64_t now_us,
                                  double render_fps,
                                  double decode_fps,
                                  double rx_kbps) {
    if (!ctx || !ctx->health_file || !*ctx->health_file) return;
    char tmp_path[PATH_MAX];
    if (snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", ctx->health_file) >= (int)sizeof(tmp_path)) return;
    const double rtp_age_sec = ctx->last_media_arrival_us > 0
                                   ? (now_us - ctx->last_media_arrival_us) / 1000000.0
                                   : -1.0;
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f,
            "{\"version\":1,\"pid\":%ld,\"stream\":\"%s\",\"session\":\"%s\","
            "\"updated_at\":%.3f,\"rtp_last_age_sec\":%.3f,"
            "\"rtp_packets_interval\":%" PRId64 ",\"rtp_packets_total\":%" PRId64 ","
            "\"rtp_missing_interval\":%" PRId64 ",\"rtp_missing_total\":%" PRId64 ","
            "\"render_fps\":%.3f,\"decode_fps\":%.3f,\"rx_kbps\":%.3f,\"decode_ok\":%d}\n",
            receiver_pid(),
            ctx->stream_name ? ctx->stream_name : "",
            ctx->session_id,
            (double)time(NULL),
            rtp_age_sec,
            ctx->media_packets_interval,
            ctx->media_packets_total,
            ctx->rtp_missing_packets_interval,
            ctx->rtp_missing_packets_total,
            render_fps,
            decode_fps,
            rx_kbps,
            ctx->decode_ok);
    if (fclose(f) != 0 || replace_health_file(tmp_path, ctx->health_file) != 0) {
        remove(tmp_path);
    }
}

int main(int argc, char **argv) {
    if (platform_net_init() != 0) {
        fprintf(stderr, "network init failed\n");
        return 1;
    }
    atexit(platform_net_shutdown);

    int port = 5004;
    int headless = 0;
    int start_hidden = 0;
    int overlay_enabled = 1;
    const char *sender_ip = "192.168.9.105";
    int sender_ack_port = 5005;
    TransportMode mode = TRANSPORT_MODE_DIRECT;
    const char *session_id = RELAY_DEFAULT_SESSION_ID;
    const char *positional[3];
    int pos_count = 0;
    int port_explicit = 0;
    int relay_ip_explicit = 0;
    int relay_port_explicit = 0;
    int session_explicit = 0;
    unsigned long parent_pid = 0;
    const char *stream_name = "head";
    int scale_cover = 0;
    const char *health_file = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--headless") == 0) {
            headless = 1;
            continue;
        }
        if (strcmp(argv[i], "--no-overlay") == 0) {
            overlay_enabled = 0;
            continue;
        }
        if (strcmp(argv[i], "--start-hidden") == 0) {
            start_hidden = 1;
            continue;
        }
        if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
            if (parse_transport_mode(argv[++i], &mode) != 0) {
                fprintf(stderr, "invalid --mode value: %s (expected direct|relay)\n", argv[i]);
                return 1;
            }
            continue;
        }
        if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            port = atoi(argv[++i]);
            port_explicit = 1;
            continue;
        }
        if (strcmp(argv[i], "--sender-ip") == 0 && i + 1 < argc) {
            sender_ip = argv[++i];
            continue;
        }
        if (strcmp(argv[i], "--sender-ack-port") == 0 && i + 1 < argc) {
            sender_ack_port = atoi(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--relay-ip") == 0 && i + 1 < argc) {
            sender_ip = argv[++i];
            relay_ip_explicit = 1;
            continue;
        }
        if (strcmp(argv[i], "--relay-port") == 0 && i + 1 < argc) {
            sender_ack_port = atoi(argv[++i]);
            relay_port_explicit = 1;
            continue;
        }
        if ((strcmp(argv[i], "--session") == 0 || strcmp(argv[i], "--session-id") == 0) && i + 1 < argc) {
            session_id = argv[++i];
            session_explicit = 1;
            continue;
        }
        if ((strcmp(argv[i], "--name") == 0 || strcmp(argv[i], "--stream") == 0) && i + 1 < argc) {
            stream_name = argv[++i];
            continue;
        }
        if (strcmp(argv[i], "--scale") == 0 && i + 1 < argc) {
            const char *value = argv[++i];
            if (strcmp(value, "contain") == 0) {
                scale_cover = 0;
            } else if (strcmp(value, "cover") == 0) {
                scale_cover = 1;
            } else {
                fprintf(stderr, "invalid --scale value: %s (expected contain|cover)\n", value);
                return 1;
            }
            continue;
        }
        if (strcmp(argv[i], "--health-file") == 0 && i + 1 < argc) {
            health_file = argv[++i];
            continue;
        }
        if (strcmp(argv[i], "--parent-pid") == 0 && i + 1 < argc) {
            char *end = NULL;
            parent_pid = strtoul(argv[++i], &end, 10);
            if (!end || *end != '\0' || parent_pid == 0) {
                fprintf(stderr, "invalid --parent-pid value\n");
                return 1;
            }
            continue;
        }
        if (argv[i][0] == '-' && argv[i][1] == '-') {
            fprintf(stderr, "unknown option: %s\n", argv[i]);
            return 1;
        }
        if (pos_count < (int)(sizeof(positional) / sizeof(positional[0]))) {
            positional[pos_count++] = argv[i];
        }
    }

    if (pos_count >= 1) {
        port = atoi(positional[0]);
        port_explicit = 1;
    }
    if (pos_count >= 2) sender_ip = positional[1];
    if (pos_count >= 3) sender_ack_port = atoi(positional[2]);
    if (mode == TRANSPORT_MODE_RELAY && !port_explicit) {
        port = 0;
    }
    if (mode == TRANSPORT_MODE_DIRECT && (relay_ip_explicit || relay_port_explicit || session_explicit)) {
        fprintf(stderr, "warning: relay-specific options are ignored in direct mode\n");
    }
    if (mode == TRANSPORT_MODE_RELAY && pos_count >= 3) {
        fprintf(stderr, "warning: positional sender_ack_port is treated as relay port in relay mode\n");
    }

    signal(SIGINT, handle_sigint);

    ReceiverReorderConfig reorder_config;
    receiver_reorder_config_defaults(&reorder_config);
    char profiles_path[PATH_MAX] = "";
    int profiles_status = 0;
    if (packaged_profiles_path(profiles_path, sizeof(profiles_path)) == 0) {
        profiles_status = receiver_reorder_config_load(profiles_path, &reorder_config);
    } else {
        snprintf(profiles_path, sizeof(profiles_path), "<unavailable>");
        profiles_status = -1;
    }
    if (profiles_status < 0) {
        fprintf(stderr, "[receiver] reorder config unavailable: %s; using defaults capacity=%d wait_ms=%d\n",
                profiles_path, RECEIVER_REORDER_CAPACITY_DEFAULT, RECEIVER_REORDER_WAIT_MS_DEFAULT);
    }
    if (reorder_config.capacity_invalid || reorder_config.wait_invalid) {
        fprintf(stderr, "[receiver] invalid reorder config in %s; using defaults for%s%s\n",
                profiles_path,
                reorder_config.capacity_invalid ? " reorder_capacity" : "",
                reorder_config.wait_invalid ? " reorder_wait_ms" : "");
    }

    if (parent_pid && acquire_operator_guard(parent_pid, session_id) != 0) {
        return 1;
    }

    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket");
        return 1;
    }
    tune_udp_recv_socket(sockfd);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);

    if (bind(sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        socket_close(sockfd);
        return 1;
    }
    (void)socket_set_nonblock(sockfd);

    socklen_t bound_len = sizeof(addr);
    if (getsockname(sockfd, (struct sockaddr *)&addr, &bound_len) == 0) {
        port = (int)ntohs(addr.sin_port);
    }

    const AVCodec *decoder = avcodec_find_decoder(AV_CODEC_ID_HEVC);
    if (!decoder) {
        fprintf(stderr, "HEVC decoder not found\n");
        socket_close(sockfd);
        return 1;
    }

    ReceiverContext ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.headless = headless;
    ctx.overlay_enabled = overlay_enabled && !headless;
    ctx.decode_ok = 0;
    ctx.dec_ctx = avcodec_alloc_context3(decoder);
    ctx.parser = av_parser_init(AV_CODEC_ID_HEVC);
    ctx.frame = av_frame_alloc();
    ctx.pkt = av_packet_alloc();
    ctx.last_report_us = av_gettime_relative();
    ctx.ctrl_fd = open_ctrl_socket();
    ctx.mode = mode;
    relay_session_id_copy(ctx.session_id, session_id);
    ctx.last_heartbeat_us = 0;
    memset(&ctx.ctrl_peer, 0, sizeof(ctx.ctrl_peer));
    ctx.ctrl_peer.sin_family = AF_INET;
    ctx.ctrl_peer.sin_port = htons((uint16_t)sender_ack_port);
    if (inet_pton(AF_INET, sender_ip, &ctx.ctrl_peer.sin_addr) != 1) {
        fprintf(stderr, "warning: invalid sender ip for ACK: %s\n", sender_ip);
        if (ctx.ctrl_fd >= 0 && ctx.ctrl_fd != sockfd) socket_close(ctx.ctrl_fd);
        ctx.ctrl_fd = -1;
    }
    if (mode == TRANSPORT_MODE_RELAY) {
        /* Heartbeats/ACKs must use the RTP socket so relay forwards to the correct port. */
        if (ctx.ctrl_fd >= 0 && ctx.ctrl_fd != sockfd) socket_close(ctx.ctrl_fd);
        ctx.ctrl_fd = sockfd;
    }

    if (!ctx.dec_ctx || !ctx.parser || !ctx.frame || !ctx.pkt) {
        fprintf(stderr, "alloc decode context failed\n");
        socket_close(sockfd);
        return 1;
    }
    ctx.dec_ctx->flags |= AV_CODEC_FLAG_LOW_DELAY;

    if (avcodec_open2(ctx.dec_ctx, decoder, NULL) < 0) {
        fprintf(stderr, "avcodec_open2 failed\n");
        socket_close(sockfd);
        return 1;
    }

    ctx.nalu_buf_cap = NALU_BUF_BYTES;
    ctx.nalu_buf = (uint8_t *)malloc((size_t)ctx.nalu_buf_cap);
    if (!ctx.nalu_buf) {
        fprintf(stderr, "nalu buffer alloc failed\n");
        socket_close(sockfd);
        return 1;
    }

    ctx.pi_net_rtt_sample_next = 0;
    ctx.stream_name = stream_name;
    ctx.scale_cover = scale_cover;
    ctx.health_file = health_file;

    if (!ctx.headless) {
        if (SDL_Init(SDL_INIT_VIDEO) != 0) {
            fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
            socket_close(sockfd);
            return 1;
        }

        Uint32 window_flags = SDL_WINDOW_RESIZABLE;
        if (start_hidden) {
            window_flags |= SDL_WINDOW_HIDDEN;
        } else {
            window_flags |= SDL_WINDOW_SHOWN;
        }

        char window_title[128];
        snprintf(window_title, sizeof(window_title), "RTP H265 Receiver [%s]", ctx.stream_name);
        ctx.window = SDL_CreateWindow(window_title,
                                      SDL_WINDOWPOS_CENTERED,
                                      SDL_WINDOWPOS_CENTERED,
                                      1280,
                                      720,
                                      window_flags);
        ctx.renderer = SDL_CreateRenderer(ctx.window, -1, SDL_RENDERER_ACCELERATED);
        if (!ctx.window || !ctx.renderer) {
            fprintf(stderr, "SDL window/renderer create failed: %s\n", SDL_GetError());
            socket_close(sockfd);
            return 1;
        }
        init_overlay(&ctx);
    }

    ctx.rtp_handler.alloc = rtp_alloc;
    ctx.rtp_handler.free = rtp_free;
    ctx.rtp_handler.packet = rtp_on_nalu;
    rtp_loss_recovery_init(&ctx.loss_recovery, ctx.decode_ok);
    ctx.rtp_unpacker = rtp_payload_decode_create(RTP_PAYLOAD_H265, "H265", &ctx.rtp_handler, &ctx);
    if (!ctx.rtp_unpacker) {
        fprintf(stderr, "rtp_payload_decode_create failed\n");
        socket_close(sockfd);
        return 1;
    }
    RtpReorderHooks reorder_hooks = {.user = &ctx, .emit = ordered_rtp_emit, .missing = ordered_rtp_missing};
    rtp_reorder_init(&ctx.reorder,
                     &reorder_hooks,
                     reorder_config.reorder_capacity,
                     (int64_t)reorder_config.reorder_wait_ms * 1000);

    VideoFecConfig fec_cfg;
    video_fec_parse_env(&fec_cfg);
    VideoFecRecvHooks fec_hooks = {
        .user = &ctx,
        .emit = fec_emit_cb,
        .deep = fec_deep_cb,
        .rtp_seq_ready = &ctx.rtp_seq_ready,
        .last_rtp_seq = &ctx.last_rtp_seq,
    };
    ctx.fec = NULL;
    if (fec_cfg.enabled) {
        ctx.fec = video_fec_receiver_create(&fec_cfg, &fec_hooks);
        if (!ctx.fec) {
            fprintf(stderr, "video_fec_receiver_create failed\n");
            rtp_payload_decode_destroy(ctx.rtp_unpacker);
            socket_close(sockfd);
            return 1;
        }
    }

    printf("receiver start: mode=%s session=%s udp :%d ack_target=%s:%d fec=%d k=%d timeout_ms=%d reorder_capacity=%d reorder_wait_ms=%d reorder_config=%s config_status=%d\n",
           transport_mode_name(mode),
           mode == TRANSPORT_MODE_RELAY ? ctx.session_id : "-",
           port,
           sender_ip,
           sender_ack_port,
           fec_cfg.enabled,
           fec_cfg.k,
           fec_cfg.timeout_ms,
           ctx.reorder.capacity,
           reorder_config.reorder_wait_ms,
           profiles_path,
           profiles_status);
    fflush(stdout);

    while (!g_stop) {
        if (!operator_parent_alive()) {
            g_stop = 1;
            break;
        }
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_QUIT) g_stop = 1;
        }

        uint8_t packet[MAX_RTP_PACKET];
        // The socket is made non-blocking above. MSG_DONTWAIT is Linux-specific
        // and can make Winsock reject recvfrom even on a valid UDP socket.
        ssize_t n = recvfrom(sockfd, packet, sizeof(packet), 0, NULL, NULL);
        int64_t now = av_gettime_relative();
        if (n < 0) {
            if (socket_would_block()) {
                SDL_Delay(1);
            } else {
                fprintf(stderr, "recvfrom failed (socket error=%d)\n", socket_last_error());
                break;
            }
        } else {
            const uint8_t *payload = packet;
            ssize_t payload_len = n;
            int skip_payload = 0;
            if (mode == TRANSPORT_MODE_RELAY) {
                RelayHeader hdr;
                if (relay_header_read(packet, (size_t)n, &hdr)) {
                    const uint8_t *relay_payload = packet + RELAY_HEADER_SIZE;
                    ssize_t relay_payload_len = n - RELAY_HEADER_SIZE;
                    if (strcmp(hdr.session_id, ctx.session_id) != 0) {
                        skip_payload = 1;
                    } else if (hdr.type == RELAY_TYPE_SENDER_STATS) {
                        if (relay_payload_len == (ssize_t)sizeof(FrameAckStats)) {
                            FrameAckStats stats_pkt;
                            memcpy(&stats_pkt, relay_payload, sizeof(stats_pkt));
                            apply_pi_net_stats(&ctx, &stats_pkt, now);
                        }
                        skip_payload = 1;
                    } else if (hdr.type != RELAY_TYPE_RTP) {
                        skip_payload = 1;
                    } else {
                        payload = relay_payload;
                        payload_len = relay_payload_len;
                    }
                } else if (n == (ssize_t)sizeof(FrameAckStats)) {
                    /* relay forwards sender stats payload without relay header (same as RTP). */
                    FrameAckStats stats_pkt;
                    memcpy(&stats_pkt, packet, sizeof(stats_pkt));
                    if (frame_ack_stats_valid(&stats_pkt)) {
                        apply_pi_net_stats(&ctx, &stats_pkt, now);
                        skip_payload = 1;
                    }
                }
            }
            if (!skip_payload) {
                ctx.rx_bytes_interval += payload_len;
                note_media_packet_arrival(&ctx, payload, payload_len, now);
                if (ctx.fec && ctx.fec->enabled) {
                    video_fec_receiver_input(ctx.fec, payload, (int)payload_len, now);
                } else {
                    rtp_reorder_input(&ctx.reorder, payload, (int)payload_len, now);
                }
            }
        }
        if (ctx.fec && ctx.fec->enabled) {
            video_fec_receiver_poll_timeouts(ctx.fec, now);
        } else {
            rtp_reorder_poll(&ctx.reorder, now);
            apply_pending_rtp_loss(&ctx);
        }
        if (mode == TRANSPORT_MODE_RELAY && now - ctx.last_heartbeat_us >= 1000000) {
            send_heartbeat(&ctx);
            ctx.last_heartbeat_us = now;
        }
        if (now - ctx.last_report_us >= 2000000) {
            double elapsed_s = (now - ctx.last_report_us) / 1000000.0;
            double fps = (double)ctx.rendered_interval / elapsed_s;
            double decode_fps = (double)ctx.decoded_interval / elapsed_s;
            double decode_ms = ctx.decoded_interval > 0 ? (ctx.decode_us_interval / (double)ctx.decoded_interval) / 1000.0 : 0.0;
            double rx_kbps = (ctx.rx_bytes_interval * 8.0) / (elapsed_s * 1000.0);
            int pending = 0;
            double wait_avg_ms = 0.0;
            double wait_max_ms = 0.0;
            if (ctx.fec && ctx.fec->enabled) {
                for (int i = 0; i < VIDEO_FEC_MAX_PENDING; i++) {
                    if (ctx.fec->blocks[i].in_use) pending++;
                }
                if (ctx.fec->stats.fec_closed_blocks > 0) {
                    wait_avg_ms = (ctx.fec->stats.fec_wait_us_sum / (double)ctx.fec->stats.fec_closed_blocks) / 1000.0;
                }
                wait_max_ms = ctx.fec->stats.fec_wait_us_max / 1000.0;
            }
            double pi_rtt_age_ms =
                ctx.pi_net_rtt_last_us > 0 ? (double)(now - ctx.pi_net_rtt_last_us) / 1000.0 : -1.0;
            printf("[receiver] render_fps=%.2f decode_fps=%.2f decode_ms=%.3f nalu=%" PRId64 " rx_kbps=%.1f vps=%" PRId64 " sps=%" PRId64 " pps=%" PRId64 " idr=%" PRId64 " cra=%" PRId64 " decode_ok=%d recovery=%" PRId64 " decode_loss=%" PRId64 " keyframe_req=%" PRId64 " rtp_seq_gap=%" PRId64 " rtp_reordered=%" PRId64 " rtp_missing=%" PRId64 " rtp_late_drop=%" PRId64 " reorder_pending=%d reorder_wait_ms_avg=%.3f reorder_wait_ms_max=%.3f damaged_nalu_drop=%" PRId64 " last_seq=%u last_ts=%" PRIu32
                   " pi_net_rtt_last=%.3f pi_net_rtt_avg=%.3f pi_net_rtt_age_ms=%.0f"
                   " fec_enabled=%d fec_k=%d fec_timeout_ms=%d fec_recv=%" PRId64 " fec_recovered=%" PRId64 " fec_fail=%" PRId64 " fec_pending=%d fec_wait_ms_avg=%.3f fec_wait_ms_max=%.3f fec_block_closed_by_new_block=%" PRId64 "\n",
                   fps,
                   decode_fps,
                   decode_ms,
                   ctx.nalu_interval,
                   rx_kbps,
                   ctx.vps_interval,
                   ctx.sps_interval,
                   ctx.pps_interval,
                   ctx.idr_interval,
                   ctx.cra_interval,
                   ctx.decode_ok,
                   ctx.recovery_interval,
                   ctx.decode_loss_interval,
                   ctx.keyframe_req_interval,
                   ctx.reorder.gaps_interval,
                   ctx.reorder.reordered_interval,
                   ctx.reorder.missing_interval,
                   ctx.reorder.late_drop_interval,
                   rtp_reorder_count(&ctx.reorder),
                   ctx.reorder.reordered_interval + ctx.reorder.missing_interval > 0 ?
                       (ctx.reorder.wait_us_sum_interval / (double)(ctx.reorder.reordered_interval + ctx.reorder.missing_interval)) / 1000.0 : 0.0,
                   ctx.reorder.wait_us_max_interval / 1000.0,
                   ctx.damaged_nalu_drop_interval,
                   (unsigned)ctx.last_rtp_seq,
                   ctx.last_rtp_packet_ts,
                   ctx.pi_net_rtt_ms_last,
                   ctx.pi_net_rtt_ms_avg,
                   pi_rtt_age_ms,
                   ctx.fec ? 1 : 0,
                   ctx.fec ? ctx.fec->k : 0,
                   ctx.fec ? ctx.fec->timeout_ms : 0,
                   ctx.fec ? ctx.fec->stats.fec_recv : 0,
                   ctx.fec ? ctx.fec->stats.fec_recovered : 0,
                   ctx.fec ? ctx.fec->stats.fec_fail : 0,
                   pending,
                   wait_avg_ms,
                   wait_max_ms,
                   ctx.fec ? ctx.fec->stats.fec_block_closed_by_new_block : 0);
            fflush(stdout);
            write_receiver_health(&ctx, now, fps, decode_fps, rx_kbps);
            ctx.last_report_us = now;
            ctx.rendered_interval = 0;
            ctx.nalu_interval = 0;
            ctx.rx_bytes_interval = 0;
            ctx.vps_interval = 0;
            ctx.sps_interval = 0;
            ctx.pps_interval = 0;
            ctx.idr_interval = 0;
            ctx.cra_interval = 0;
            ctx.decoded_interval = 0;
            ctx.decode_us_interval = 0.0;
            ctx.media_packets_interval = 0;
            ctx.rtp_missing_packets_interval = 0;
            ctx.recovery_interval = 0;
            ctx.decode_loss_interval = 0;
            ctx.keyframe_req_interval = 0;
            ctx.rtp_seq_gap_interval = 0;
            ctx.damaged_nalu_drop_interval = 0;
            rtp_reorder_reset_interval_stats(&ctx.reorder);
            if (ctx.fec) video_fec_receiver_reset_interval_stats(ctx.fec);
        }
    }

    rtp_payload_decode_destroy(ctx.rtp_unpacker);
    video_fec_receiver_destroy(ctx.fec);
    ctx.fec = NULL;
    if (ctx.texture) SDL_DestroyTexture(ctx.texture);
    destroy_overlay(&ctx);
    if (ctx.renderer) SDL_DestroyRenderer(ctx.renderer);
    if (ctx.window) SDL_DestroyWindow(ctx.window);
    SDL_Quit();

    if (ctx.yuv420) av_frame_free(&ctx.yuv420);
    if (ctx.sws) sws_freeContext(ctx.sws);
    if (ctx.nalu_buf) free(ctx.nalu_buf);
    av_packet_free(&ctx.pkt);
    av_frame_free(&ctx.frame);
    av_parser_close(ctx.parser);
    avcodec_free_context(&ctx.dec_ctx);

    if (ctx.ctrl_fd >= 0 && ctx.ctrl_fd != sockfd) socket_close(ctx.ctrl_fd);
    socket_close(sockfd);
    release_operator_guard();
    return 0;
}
