#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <libavcodec/avcodec.h>
#include <libavcodec/bsf.h>
#include <libavutil/avutil.h>
#include <libavutil/frame.h>
#include <libavutil/opt.h>
#include <libavutil/time.h>
#include <linux/videodev2.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <libswscale/swscale.h>

#include "fec.h"
#include "frame_ack.h"
#include "d405_capture.h"
#include "deployment_guard.h"
#include "encode_cadence.h"
#include "libcamera_capture.h"
#include "relay_proto.h"
#include "rtp-profile.h"
#include "rtp-payload.h"

#define CLEAR(x) memset(&(x), 0, sizeof(x))
#define BUFFER_COUNT 4
#define RTP_CLOCK 90000
#define MAX_UDP_PACKET 65536
#define STATS_REPORT_INTERVAL_US 200000
#define RTP_PACKET_SIZE 1200

static volatile sig_atomic_t g_stop = 0;

static void handle_sigint(int sig) {
    (void)sig;
    g_stop = 1;
}

typedef struct {
    void *start;
    size_t length;
} V4L2Buffer;

typedef struct {
    int64_t last_report_us;
    int64_t interval_us;
    int64_t encoded_frames_interval;
    int64_t encoded_frames_total;
    double encode_us_interval;
    double sws_us_interval;
    double enc_us_interval;
    int64_t dropped_frames_total;
    int64_t queue_drop_interval;
    int64_t max_queue_drop_interval;
    int64_t vps_interval;
    int64_t sps_interval;
    int64_t pps_interval;
    int64_t idr_interval;
    int64_t cra_interval;
    int64_t net_acked_interval;
    double net_rtt_ms_last;
    double net_rtt_ms_sum;
    int64_t decode_acked_interval;
    double decode_rtt_ms_last;
    double decode_rtt_ms_sum;
    int64_t fec_sent_interval;
    int64_t stats_tx_interval;
    int64_t ingress_ack_interval;
    int64_t ingress_ack_total;
    int64_t last_ingress_ack_us;
} Stats;

typedef enum {
    TRANSPORT_MODE_DIRECT = 0,
    TRANSPORT_MODE_RELAY = 1,
} TransportMode;

typedef struct {
    int sockfd;
    struct sockaddr_in peer;
    TransportMode mode;
    char session_id[RELAY_SESSION_ID_STR_SIZE];
    VideoFecSender *fec;
    Stats *stats;
} RtpSenderContext;

typedef struct {
    uint32_t frame_id;
    int64_t send_us;
} FrameSendEntry;

#define FRAME_MAP_SIZE 8192

static const char *transport_mode_name(TransportMode mode) {
    return mode == TRANSPORT_MODE_RELAY ? "relay" : "direct";
}

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

static int apply_input_option(const char *input, const char **device) {
    if (!input || !device) return 0;
    if (strcmp(input, "v4l2") == 0) return 0;
    if (strcmp(input, "libcamera") == 0 || (strcmp(input, "d405") == 0 || strcmp(input, "realsense") == 0) || strcmp(input, "fake") == 0) {
        *device = input;
        return 0;
    }
    return -1;
}

static int open_udp_ack_socket(int port) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    return fd;
}

static void fill_fake_yuv420p(AVFrame *f, int frame_idx) {
    const int w = f->width;
    const int h = f->height;
    for (int y = 0; y < h; y++) {
        uint8_t *row = f->data[0] + y * f->linesize[0];
        for (int x = 0; x < w; x++) {
            row[x] = (uint8_t)((x + y + frame_idx * 2) & 0xFF);
        }
    }
    for (int y = 0; y < h / 2; y++) {
        uint8_t *u = f->data[1] + y * f->linesize[1];
        uint8_t *v = f->data[2] + y * f->linesize[2];
        for (int x = 0; x < w / 2; x++) {
            u[x] = (uint8_t)((96 + frame_idx * 3 + y) & 0xFF);
            v[x] = (uint8_t)((160 + frame_idx * 2 + x) & 0xFF);
        }
    }
}

static void print_fferr(const char *prefix, int err) {
    char errbuf[AV_ERROR_MAX_STRING_SIZE] = {0};
    av_make_error_string(errbuf, sizeof(errbuf), err);
    fprintf(stderr, "%s: %s\n", prefix, errbuf);
}

static void write_sender_health(const char *path,
                                TransportMode mode,
                                const char *session_id,
                                const Stats *s,
                                double fps,
                                int64_t now_us) {
    if (!path || !*path || !s) return;
    char tmp_path[1024];
    if (snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path) >= (int)sizeof(tmp_path)) return;
    const double ingress_age = s->last_ingress_ack_us > 0
                                   ? (now_us - s->last_ingress_ack_us) / 1000000.0
                                   : -1.0;
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f,
            "{\"version\":1,\"pid\":%ld,\"mode\":\"%s\",\"session\":\"%s\","
            "\"updated_at\":%.6f,\"encoded_fps\":%.3f,\"encoded_total\":%" PRId64 ","
            "\"dropped_total\":%" PRId64 ",\"relay_ingress_ack_interval\":%" PRId64 ","
            "\"relay_ingress_ack_total\":%" PRId64 ",\"relay_ingress_ack_age_sec\":%.3f}\n",
            (long)getpid(),
            transport_mode_name(mode),
            session_id ? session_id : "",
            now_us / 1000000.0,
            fps,
            s->encoded_frames_total,
            s->dropped_frames_total,
            s->ingress_ack_interval,
            s->ingress_ack_total,
            ingress_age);
    if (fclose(f) != 0) {
        unlink(tmp_path);
        return;
    }
    if (rename(tmp_path, path) != 0) unlink(tmp_path);
}

static void report_stats_if_needed(Stats *s,
                                   TransportMode mode,
                                   const char *session_id,
                                   const char *health_file) {
    int64_t now = av_gettime_relative();
    if (now - s->last_report_us < s->interval_us) {
        return;
    }

    double elapsed_s = (double)(now - s->last_report_us) / 1000000.0;
    double fps = elapsed_s > 0.0 ? (double)s->encoded_frames_interval / elapsed_s : 0.0;
    double avg_encode_ms = s->encoded_frames_interval > 0
                               ? (s->encode_us_interval / (double)s->encoded_frames_interval) / 1000.0
                               : 0.0;
    double avg_sws_ms = s->encoded_frames_interval > 0
                            ? (s->sws_us_interval / (double)s->encoded_frames_interval) / 1000.0
                            : 0.0;
    double avg_enc_ms = s->encoded_frames_interval > 0
                            ? (s->enc_us_interval / (double)s->encoded_frames_interval) / 1000.0
                            : 0.0;

    double net_rtt_avg = s->net_acked_interval > 0 ? s->net_rtt_ms_sum / (double)s->net_acked_interval : 0.0;
    double decode_rtt_avg = s->decode_acked_interval > 0 ? s->decode_rtt_ms_sum / (double)s->decode_acked_interval : 0.0;
    printf("[stats] fps=%.2f avg_encode=%.3f ms avg_sws=%.3f ms avg_enc=%.3f ms encoded_total=%" PRId64
           " dropped_total=%" PRId64 " queue_drop=%" PRId64 " queue_drop_max=%" PRId64
            " net_acked=%" PRId64 " net_rtt_ms_last=%.3f net_rtt_ms_avg=%.3f"
            " decode_acked=%" PRId64 " decode_rtt_ms_last=%.3f decode_rtt_ms_avg=%.3f"
            " stats_tx=%" PRId64
            " relay_ingress_ack=%" PRId64 " relay_ingress_ack_age_ms=%.0f"
            " vps=%" PRId64 " sps=%" PRId64 " pps=%" PRId64 " idr=%" PRId64 " cra=%" PRId64
           " fec_sent=%" PRId64 "\n",
           fps,
           avg_encode_ms,
           avg_sws_ms,
           avg_enc_ms,
           s->encoded_frames_total,
           s->dropped_frames_total,
           s->queue_drop_interval,
           s->max_queue_drop_interval,
           s->net_acked_interval,
           s->net_rtt_ms_last,
           net_rtt_avg,
           s->decode_acked_interval,
            s->decode_rtt_ms_last,
            decode_rtt_avg,
            s->stats_tx_interval,
            s->ingress_ack_interval,
            s->last_ingress_ack_us > 0 ? (now - s->last_ingress_ack_us) / 1000.0 : -1.0,
            s->vps_interval,
           s->sps_interval,
           s->pps_interval,
           s->idr_interval,
            s->cra_interval,
            s->fec_sent_interval);
    fflush(stdout);

    write_sender_health(health_file, mode, session_id, s, fps, now);

    s->last_report_us = now;
    s->encoded_frames_interval = 0;
    s->encode_us_interval = 0.0;
    s->sws_us_interval = 0.0;
    s->enc_us_interval = 0.0;
    s->queue_drop_interval = 0;
    s->max_queue_drop_interval = 0;
    s->vps_interval = 0;
    s->sps_interval = 0;
    s->pps_interval = 0;
    s->idr_interval = 0;
    s->cra_interval = 0;
    s->net_acked_interval = 0;
    s->net_rtt_ms_sum = 0.0;
    s->decode_acked_interval = 0;
    s->decode_rtt_ms_sum = 0.0;
    s->fec_sent_interval = 0;
    s->stats_tx_interval = 0;
    s->ingress_ack_interval = 0;
}

static void frame_map_store(FrameSendEntry *m, uint32_t frame_id, int64_t send_us) {
    m[frame_id % FRAME_MAP_SIZE].frame_id = frame_id;
    m[frame_id % FRAME_MAP_SIZE].send_us = send_us;
}

static int send_udp_payload(RtpSenderContext *ctx, const uint8_t *packet, int bytes) {
    if (ctx->mode == TRANSPORT_MODE_RELAY) {
        uint8_t buf[MAX_UDP_PACKET];
        if (bytes < 0 || (size_t)bytes + RELAY_HEADER_SIZE > sizeof(buf)) return -1;
        relay_header_write(buf, RELAY_TYPE_RTP, ctx->session_id);
        memcpy(buf + RELAY_HEADER_SIZE, packet, (size_t)bytes);
        int r = (int)sendto(ctx->sockfd,
                            buf,
                            (size_t)bytes + RELAY_HEADER_SIZE,
                            0,
                            (const struct sockaddr *)&ctx->peer,
                            sizeof(ctx->peer));
        return (r == bytes + (int)RELAY_HEADER_SIZE) ? 0 : -1;
    }
    int r = (int)sendto(ctx->sockfd, packet, (size_t)bytes, 0, (const struct sockaddr *)&ctx->peer, sizeof(ctx->peer));
    return (r == bytes) ? 0 : -1;
}

static void poll_frame_ack_once(int ack_fd,
                                FrameSendEntry *m,
                                Stats *stats,
                                int *force_idr,
                                TransportMode mode,
                                const char *session_id,
                                RtpSenderContext *rtp_ctx) {
    if (ack_fd < 0) return;
    while (1) {
        uint8_t buf[RELAY_HEADER_SIZE + sizeof(FrameAck)];
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        ssize_t n = recvfrom(ack_fd, buf, sizeof(buf), 0, (struct sockaddr *)&from, &from_len);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            return;
        }
        const uint8_t *payload = buf;
        ssize_t payload_len = n;
        if (mode == TRANSPORT_MODE_RELAY) {
            RelayHeader hdr;
            if (relay_header_read(buf, (size_t)n, &hdr)) {
                if (session_id && *session_id && strcmp(hdr.session_id, session_id) != 0) continue;
                if (hdr.type == RELAY_TYPE_INGRESS_ACK) {
                    int64_t now = av_gettime_relative();
                    stats->ingress_ack_interval++;
                    stats->ingress_ack_total++;
                    stats->last_ingress_ack_us = now;
                    continue;
                }
                if (hdr.type != RELAY_TYPE_HEARTBEAT) continue;
                payload = buf + RELAY_HEADER_SIZE;
                payload_len = n - RELAY_HEADER_SIZE;
            }
        }
        if (payload_len != (ssize_t)sizeof(FrameAck)) continue;
        FrameAck ack;
        memcpy(&ack, payload, sizeof(ack));
        if (ack.magic != FRAME_ACK_MAGIC) continue;
        if (ack.kind == FRAME_ACK_KIND_KEYFRAME_REQ) {
            if (rtp_ctx && rtp_ctx->fec && rtp_ctx->fec->enabled) {
                uint8_t fec_pkt[12 + 4 + 2 * VIDEO_FEC_K_MAX + VIDEO_FEC_MAX_PKT];
                int fec_len = 0;
                if (video_fec_sender_flush(rtp_ctx->fec, fec_pkt, &fec_len, (int)sizeof(fec_pkt)) == 0 &&
                    fec_len > 0) {
                    if (send_udp_payload(rtp_ctx, fec_pkt, fec_len) == 0 && stats) {
                        stats->fec_sent_interval++;
                    }
                }
            }
            *force_idr = 1;
            continue;
        }
        FrameSendEntry e = m[ack.frame_id % FRAME_MAP_SIZE];
        if (e.frame_id != ack.frame_id || e.send_us <= 0) continue;
        int64_t now = av_gettime_relative();
        if (now <= e.send_us) continue;
        double rtt_ms = (now - e.send_us) / 1000.0;
        if (ack.kind == FRAME_ACK_KIND_NETWORK) {
            stats->net_acked_interval++;
            stats->net_rtt_ms_last = rtt_ms;
            stats->net_rtt_ms_sum += rtt_ms;
        } else if (ack.kind == FRAME_ACK_KIND_DECODE) {
            stats->decode_acked_interval++;
            stats->decode_rtt_ms_last = rtt_ms;
            stats->decode_rtt_ms_sum += rtt_ms;
        }
    }
}

static int stats_report_interval_us(void) {
    const char *env = getenv("STATS_REPORT_INTERVAL_MS");
    if (!env || !*env) return STATS_REPORT_INTERVAL_US;
    int ms = atoi(env);
    if (ms < 50) ms = 50;
    return ms * 1000;
}

static void maybe_send_stats_report(RtpSenderContext *ctx,
                                    Stats *stats,
                                    int64_t now_us,
                                    uint32_t *seq,
                                    int64_t *last_send_us) {
    if (!ctx || ctx->mode != TRANSPORT_MODE_RELAY || !stats || !seq || !last_send_us) return;
    if (now_us - *last_send_us < stats_report_interval_us()) return;
    if (stats->net_acked_interval <= 0 && stats->net_rtt_ms_last <= 0.0) return;

    double net_avg = stats->net_acked_interval > 0
                         ? stats->net_rtt_ms_sum / (double)stats->net_acked_interval
                         : stats->net_rtt_ms_last;

    FrameAckStats pkt;
    pkt.magic = FRAME_ACK_MAGIC;
    pkt.kind = FRAME_ACK_KIND_STATS;
    pkt.seq = ++(*seq);
    pkt.net_rtt_last_x10 = frame_ack_rtt_ms_to_x10(stats->net_rtt_ms_last);
    pkt.net_rtt_avg_x10 = frame_ack_rtt_ms_to_x10(net_avg);
    pkt.reserved = 0;

    uint8_t buf[RELAY_HEADER_SIZE + sizeof(pkt)];
    relay_header_write(buf, RELAY_TYPE_SENDER_STATS, ctx->session_id);
    memcpy(buf + RELAY_HEADER_SIZE, &pkt, sizeof(pkt));
    ssize_t sent = sendto(ctx->sockfd,
                          buf,
                          sizeof(buf),
                          0,
                          (const struct sockaddr *)&ctx->peer,
                          sizeof(ctx->peer));
    if (sent == (ssize_t)sizeof(buf)) {
        *last_send_us = now_us;
        stats->stats_tx_interval++;
    }
}

static void apply_force_idr(AVCodecContext *enc_ctx, AVFrame *enc_frame) {
    enc_frame->pict_type = AV_PICTURE_TYPE_I;
    if (enc_ctx->priv_data) {
        av_opt_set(enc_ctx->priv_data, "forced-idr", "1", 0);
    }
}

static void clear_force_idr(AVCodecContext *enc_ctx, AVFrame *enc_frame) {
    enc_frame->pict_type = AV_PICTURE_TYPE_NONE;
    if (enc_ctx->priv_data) {
        av_opt_set(enc_ctx->priv_data, "forced-idr", "0", 0);
    }
}

static int packet_has_annexb_start(const uint8_t *data, int size) {
    if (size < 4) return 0;
    return data[0] == 0 && data[1] == 0 && data[2] == 0 && data[3] == 1;
}

static size_t nv12_frame_bytes(int y_stride, int height) {
    return (size_t)y_stride * (size_t)height * 3u / 2u;
}

static int ensure_nv12_buf(uint8_t **buf, size_t *cap, int y_stride, int height) {
    size_t need = nv12_frame_bytes(y_stride, height);
    if (*cap < need) {
        uint8_t *next = realloc(*buf, need);
        if (!next) return -1;
        *buf = next;
        *cap = need;
    }
    return 0;
}

static int probe_encoder_annexb(AVCodecContext *enc_ctx, AVFrame *frame, AVPacket *pkt) {
    fill_fake_yuv420p(frame, 0);
    frame->pts = 0;
    apply_force_idr(enc_ctx, frame);

    int ret = avcodec_send_frame(enc_ctx, frame);
    if (ret < 0) return 0;

    ret = avcodec_receive_packet(enc_ctx, pkt);
    if (ret < 0) return 0;

    int has_annexb = packet_has_annexb_start(pkt->data, pkt->size);
    av_packet_unref(pkt);
    while (avcodec_receive_packet(enc_ctx, pkt) == 0) {
        av_packet_unref(pkt);
    }
    clear_force_idr(enc_ctx, frame);
    return has_annexb;
}

static void count_h265_annexb_nalus(const uint8_t *data, int bytes, Stats *s) {
    int i = 0;
    while (i + 5 < bytes) {
        int sc = 0;
        if (i + 4 < bytes && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0 && data[i + 3] == 1) {
            sc = 4;
        } else if (i + 3 < bytes && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1) {
            sc = 3;
        }
        if (!sc) {
            i++;
            continue;
        }
        int nal_start = i + sc;
        if (nal_start >= bytes) break;
        int nal_type = (data[nal_start] >> 1) & 0x3F;
        if (nal_type == 32) s->vps_interval++;
        if (nal_type == 33) s->sps_interval++;
        if (nal_type == 34) s->pps_interval++;
        if (nal_type == 19 || nal_type == 20) s->idr_interval++;
        if (nal_type == 21) s->cra_interval++;
        i = nal_start + 1;
    }
}

static int xioctl(int fd, unsigned long request, void *arg) {
    int r;
    do {
        r = ioctl(fd, request, arg);
    } while (r == -1 && errno == EINTR);
    return r;
}

static int init_v4l2(const char *device,
                     int width,
                     int height,
                     int fps,
                     int *out_fd,
                     V4L2Buffer **out_bufs,
                     int *out_buf_count,
                     struct v4l2_format *out_fmt) {
    int fd = open(device, O_RDWR | O_NONBLOCK, 0);
    if (fd < 0) {
        perror("open v4l2 device failed");
        return -1;
    }

    struct v4l2_capability cap;
    if (xioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {
        perror("VIDIOC_QUERYCAP failed");
        close(fd);
        return -1;
    }

    if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE) || !(cap.capabilities & V4L2_CAP_STREAMING)) {
        fprintf(stderr, "V4L2 capture/streaming not supported\n");
        close(fd);
        return -1;
    }

    struct v4l2_format fmt;
    CLEAR(fmt);
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = (uint32_t)width;
    fmt.fmt.pix.height = (uint32_t)height;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
    fmt.fmt.pix.field = V4L2_FIELD_ANY;

    if (xioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
        perror("VIDIOC_S_FMT failed");
        close(fd);
        return -1;
    }

    struct v4l2_streamparm parm;
    CLEAR(parm);
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = fps;
    xioctl(fd, VIDIOC_S_PARM, &parm);

    struct v4l2_requestbuffers req;
    CLEAR(req);
    req.count = BUFFER_COUNT;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;

    if (xioctl(fd, VIDIOC_REQBUFS, &req) < 0 || req.count < 2) {
        perror("VIDIOC_REQBUFS failed");
        close(fd);
        return -1;
    }

    V4L2Buffer *bufs = calloc(req.count, sizeof(*bufs));
    if (!bufs) {
        close(fd);
        return -1;
    }

    for (uint32_t i = 0; i < req.count; i++) {
        struct v4l2_buffer buf;
        CLEAR(buf);
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        if (xioctl(fd, VIDIOC_QUERYBUF, &buf) < 0) {
            perror("VIDIOC_QUERYBUF failed");
            free(bufs);
            close(fd);
            return -1;
        }

        bufs[i].length = buf.length;
        bufs[i].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buf.m.offset);
        if (bufs[i].start == MAP_FAILED) {
            perror("mmap failed");
            for (uint32_t j = 0; j < i; j++) munmap(bufs[j].start, bufs[j].length);
            free(bufs);
            close(fd);
            return -1;
        }
    }

    for (uint32_t i = 0; i < req.count; i++) {
        struct v4l2_buffer buf;
        CLEAR(buf);
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        if (xioctl(fd, VIDIOC_QBUF, &buf) < 0) {
            perror("VIDIOC_QBUF failed");
            for (uint32_t j = 0; j < req.count; j++) munmap(bufs[j].start, bufs[j].length);
            free(bufs);
            close(fd);
            return -1;
        }
    }

    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(fd, VIDIOC_STREAMON, &type) < 0) {
        perror("VIDIOC_STREAMON failed");
        for (uint32_t j = 0; j < req.count; j++) munmap(bufs[j].start, bufs[j].length);
        free(bufs);
        close(fd);
        return -1;
    }

    *out_fd = fd;
    *out_bufs = bufs;
    *out_buf_count = (int)req.count;
    *out_fmt = fmt;
    return 0;
}

static void deinit_v4l2(int fd, V4L2Buffer *bufs, int buf_count) {
    if (fd >= 0) {
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        xioctl(fd, VIDIOC_STREAMOFF, &type);
    }
    if (bufs) {
        for (int i = 0; i < buf_count; i++) {
            if (bufs[i].start && bufs[i].start != MAP_FAILED) munmap(bufs[i].start, bufs[i].length);
        }
        free(bufs);
    }
    if (fd >= 0) close(fd);
}

/* Drain queued V4L2 buffers; keep only newest frame to reduce latency growth. */
static int v4l2_drain_to_latest(int fd, struct v4l2_buffer *out_buf, int *dropped) {
    int got = 0;
    *dropped = 0;
    struct v4l2_buffer b;
    while (1) {
        CLEAR(b);
        b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        b.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd, VIDIOC_DQBUF, &b) < 0) {
            if (errno == EAGAIN) break;
            return -1;
        }
        if (got) {
            if (xioctl(fd, VIDIOC_QBUF, out_buf) < 0) return -1;
            (*dropped)++;
        }
        *out_buf = b;
        got = 1;
    }
    return got;
}

static int create_encoder(const char *encoder_name,
                          int width,
                          int height,
                          AVRational fr,
                          int gop_size,
                          int64_t bit_rate,
                          AVCodecContext **enc_ctx) {
    int ret;
    const AVCodec *encoder = avcodec_find_encoder_by_name(encoder_name);
    if (!encoder) {
        fprintf(stderr, "Encoder '%s' not found\n", encoder_name);
        return AVERROR_ENCODER_NOT_FOUND;
    }

    *enc_ctx = avcodec_alloc_context3(encoder);
    if (!*enc_ctx) return AVERROR(ENOMEM);

    (*enc_ctx)->codec_type = AVMEDIA_TYPE_VIDEO;
    (*enc_ctx)->width = width;
    (*enc_ctx)->height = height;
    (*enc_ctx)->pix_fmt = AV_PIX_FMT_YUV420P;
    (*enc_ctx)->time_base = av_inv_q(fr);
    (*enc_ctx)->framerate = fr;
    (*enc_ctx)->gop_size = gop_size > 0 ? gop_size : 30;
    (*enc_ctx)->max_b_frames = 0;
    (*enc_ctx)->thread_count = 1;
    (*enc_ctx)->flags |= AV_CODEC_FLAG_LOW_DELAY;
    int64_t target_bit_rate = bit_rate > 0 ? bit_rate : 2 * 1000 * 1000;
    int target_kbps = (int)((target_bit_rate + 999) / 1000);
    if (target_kbps < 1) target_kbps = 1;

    /*
     * Keep VBV small for low-latency UDP. This constrains burst size,
     * including IDR/I-frame bursts, instead of only targeting average bitrate.
     */
    int vbv_buf_kbps = target_kbps / 3;
    if (vbv_buf_kbps < 100) vbv_buf_kbps = 100;

    (*enc_ctx)->bit_rate = target_bit_rate;

    AVDictionary *opts = NULL;
    char x265_params[512];
    int keyint = gop_size > 0 ? gop_size : 30;
    snprintf(x265_params,
             sizeof(x265_params),
             "bitrate=%d:vbv-maxrate=%d:vbv-bufsize=%d:strict-cbr=1:"
             "repeat-headers=1:annexb=1:open-gop=0:keyint=%d:min-keyint=%d:scenecut=0:bframes=0",
             target_kbps,
             target_kbps,
             vbv_buf_kbps,
             keyint,
             keyint);

    av_dict_set(&opts, "preset", "ultrafast", 0);
    av_dict_set(&opts, "tune", "zerolatency", 0);
    av_dict_set(&opts, "rc-lookahead", "0", 0);
    // Keep RTP stream decoder-friendly: Annex-B NALU format and in-band VPS/SPS/PPS.
    av_dict_set(&opts, "x265-params", x265_params, 0);

    ret = avcodec_open2(*enc_ctx, encoder, &opts);
    av_dict_free(&opts);
    if (ret < 0) print_fferr("avcodec_open2(encoder) failed", ret);
    return ret;
}

static void *rtp_alloc(void *param, int bytes) {
    (void)param;
    return malloc((size_t)bytes);
}

static void rtp_free(void *param, void *packet) {
    (void)param;
    free(packet);
}

static int rtp_packet_send(void *param, const void *packet, int bytes, uint32_t timestamp, int flags) {
    (void)flags;
    RtpSenderContext *ctx = (RtpSenderContext *)param;
    if (!ctx->fec || !ctx->fec->enabled) {
        return send_udp_payload(ctx, (const uint8_t *)packet, bytes);
    }

    uint8_t media_out[VIDEO_FEC_MAX_PKT];
    uint8_t fec_out[12 + 4 + 2 * VIDEO_FEC_K_MAX + VIDEO_FEC_MAX_PKT];
    int media_len = 0;
    int fec_len = 0;
    int64_t sent_before = ctx->fec->fec_sent;
    if (video_fec_sender_on_media(ctx->fec,
                                  (const uint8_t *)packet,
                                  bytes,
                                  timestamp,
                                  media_out,
                                  &media_len,
                                  (int)sizeof(media_out),
                                  fec_out,
                                  &fec_len,
                                  (int)sizeof(fec_out)) != 0) {
        return -1;
    }
    if (send_udp_payload(ctx, media_out, media_len) != 0) return -1;
    if (fec_len > 0) {
        if (send_udp_payload(ctx, fec_out, fec_len) != 0) return -1;
        if (ctx->stats && ctx->fec->fec_sent > sent_before) {
            ctx->stats->fec_sent_interval += (ctx->fec->fec_sent - sent_before);
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *device = "/dev/video0";
    const char *encoder_name = "libx265";
    const char *peer_ip = "192.168.9.106";
    const char *input_override = NULL;
    const char *session_id = RELAY_DEFAULT_SESSION_ID;
    int peer_port = 5004;
    int report_sec = 2;
    int ack_port = 5005;
    int width = 1024, height = 768, fps = 30, capture_fps = 0;
    int64_t bit_rate = 0;
    TransportMode mode = TRANSPORT_MODE_DIRECT;
    int relay_ip_explicit = 0;
    int relay_port_explicit = 0;
    int session_explicit = 0;
    const char *health_file = NULL;

    const char *positional[10];
    int pos_count = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
            if (parse_transport_mode(argv[++i], &mode) != 0) {
                fprintf(stderr, "invalid --mode value: %s (expected direct|relay)\n", argv[i]);
                return 1;
            }
            continue;
        }
        if (strcmp(argv[i], "--input") == 0 && i + 1 < argc) {
            input_override = argv[++i];
            continue;
        }
        if (strcmp(argv[i], "--width") == 0 && i + 1 < argc) {
            width = atoi(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--height") == 0 && i + 1 < argc) {
            height = atoi(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--fps") == 0 && i + 1 < argc) {
            fps = atoi(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--capture-fps") == 0 && i + 1 < argc) {
            capture_fps = atoi(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--bitrate") == 0 && i + 1 < argc) {
            bit_rate = atoll(argv[++i]);
            continue;
        }
        if (strcmp(argv[i], "--health-file") == 0 && i + 1 < argc) {
            health_file = argv[++i];
            continue;
        }
        if (strcmp(argv[i], "--relay-ip") == 0 && i + 1 < argc) {
            peer_ip = argv[++i];
            relay_ip_explicit = 1;
            continue;
        }
        if (strcmp(argv[i], "--relay-port") == 0 && i + 1 < argc) {
            peer_port = atoi(argv[++i]);
            relay_port_explicit = 1;
            continue;
        }
        if ((strcmp(argv[i], "--session") == 0 || strcmp(argv[i], "--session-id") == 0) && i + 1 < argc) {
            session_id = argv[++i];
            session_explicit = 1;
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

    if (pos_count >= 1) device = positional[0];
    if (pos_count >= 2) encoder_name = positional[1];
    if (pos_count >= 3) peer_ip = positional[2];
    if (pos_count >= 4) peer_port = atoi(positional[3]);
    if (pos_count >= 5) report_sec = atoi(positional[4]);
    if (pos_count >= 6) ack_port = atoi(positional[5]);
    if (pos_count >= 7) width = atoi(positional[6]);
    if (pos_count >= 8) height = atoi(positional[7]);
    if (pos_count >= 9) fps = atoi(positional[8]);
    if (pos_count >= 10) bit_rate = atoll(positional[9]);

    if (apply_input_option(input_override, &device) != 0) {
        fprintf(stderr, "invalid --input value: %s (expected v4l2|libcamera|d405|fake)\n", input_override);
        return 1;
    }
    if (mode == TRANSPORT_MODE_DIRECT && (relay_ip_explicit || relay_port_explicit || session_explicit)) {
        fprintf(stderr, "warning: relay-specific options are ignored in direct mode\n");
    }

    if (fps <= 0) fps = 30;
    if (capture_fps <= 0) capture_fps = fps;
    if (width <= 0) width = 1024;
    if (height <= 0) height = 768;
    if (bit_rate <= 0) bit_rate = (int64_t)2000000 * fps / 30;
    int gop_size = 15;

    signal(SIGINT, handle_sigint);

    int v4l2_fd = -1;
    V4L2Buffer *v4l2_bufs = NULL;
    int v4l2_buf_count = 0;
    struct v4l2_format v4l2_fmt;
    CLEAR(v4l2_fmt);
    int use_fake = (strcmp(device, "fake") == 0);
    int use_libcamera = (strcmp(device, "libcamera") == 0);
    int use_d405 = (strcmp(device, "d405") == 0 || strncmp(device, "d405:", 5) == 0 || strcmp(device, "realsense") == 0 || strncmp(device, "realsense:", 10) == 0);
    if (!use_fake && !yam_deployment_video_allowed()) return 2;
    int fake_frame_idx = 0;
    LibcameraCapture *lc_cap = NULL;
    D405Capture *d405_cap = NULL;
    uint8_t *src_nv12_buf = NULL;
    size_t src_nv12_cap = 0;
    enum AVPixelFormat src_pix_fmt = AV_PIX_FMT_YUYV422;

    AVCodecContext *enc_ctx = NULL;
    struct SwsContext *sws = NULL;
    AVFrame *src_frame = NULL;
    AVFrame *enc_frame = NULL;
    AVPacket *pkt = NULL;
    AVBSFContext *hevc_bsf = NULL;
    int ret = 0;

    int udp_fd = -1;
    int ack_fd = -1;
    void *rtp_packer = NULL;
    RtpSenderContext rtp_ctx;
    CLEAR(rtp_ctx);
    VideoFecConfig fec_cfg;
    video_fec_parse_env(&fec_cfg);
    VideoFecSender fec_sender;
    memset(&fec_sender, 0, sizeof(fec_sender));

    if (use_libcamera) {
        lc_cap = libcamera_capture_open(width, height, capture_fps);
        if (!lc_cap) {
            ret = -1;
            goto end;
        }
        src_pix_fmt = AV_PIX_FMT_NV12;
    } else if (use_d405) {
        const char *serial = getenv("D405_SERIAL");
        if (strncmp(device, "d405:", 5) == 0 && device[5]) {
            serial = device + 5;
        }
        if (strncmp(device, "realsense:", 10) == 0) serial = device + 10;
        if (!serial || !*serial) { fprintf(stderr, "explicit SDK serial required\n"); return 2; }
        d405_cap = d405_capture_open(width, height, capture_fps, serial);
        if (!d405_cap) {
            ret = -1;
            goto end;
        }
        src_pix_fmt = AV_PIX_FMT_YUYV422;
    } else if (!use_fake) {
        ret = init_v4l2(device, width, height, capture_fps, &v4l2_fd, &v4l2_bufs, &v4l2_buf_count, &v4l2_fmt);
        if (ret < 0) goto end;
        width = (int)v4l2_fmt.fmt.pix.width;
        height = (int)v4l2_fmt.fmt.pix.height;
        src_pix_fmt = AV_PIX_FMT_YUYV422;
    }

    AVRational fr = (AVRational){fps, 1};
    ret = create_encoder(encoder_name, width, height, fr, gop_size, bit_rate, &enc_ctx);
    if (ret < 0) goto end;

    udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd < 0) {
        perror("socket udp failed");
        ret = -1;
        goto end;
    }
    rtp_ctx.sockfd = udp_fd;
    rtp_ctx.mode = mode;
    relay_session_id_copy(rtp_ctx.session_id, session_id);
    rtp_ctx.peer.sin_family = AF_INET;
    rtp_ctx.peer.sin_port = htons((uint16_t)peer_port);
    if (inet_pton(AF_INET, peer_ip, &rtp_ctx.peer.sin_addr) != 1) {
        fprintf(stderr, "invalid peer ip: %s\n", peer_ip);
        ret = -1;
        goto end;
    }

    struct rtp_payload_t cb = {rtp_alloc, rtp_free, rtp_packet_send};
    uint32_t ssrc = (uint32_t)av_gettime_relative();
    video_fec_sender_init(&fec_sender, &fec_cfg, ssrc);
    rtp_ctx.fec = &fec_sender;
    rtp_packet_setsize(RTP_PACKET_SIZE);
    rtp_packer = rtp_payload_encode_create(RTP_PAYLOAD_H265, "H265", (uint16_t)ssrc, ssrc, &cb, &rtp_ctx);
    if (!rtp_packer) {
        fprintf(stderr, "rtp_payload_encode_create failed\n");
        ret = -1;
        goto end;
    }
    if (mode == TRANSPORT_MODE_DIRECT) {
        ack_fd = open_udp_ack_socket(ack_port);
        if (ack_fd < 0) {
            fprintf(stderr, "warning: ack socket bind failed on port %d (RTT disabled)\n", ack_port);
        }
    } else {
        int flags = fcntl(udp_fd, F_GETFL, 0);
        if (flags >= 0) fcntl(udp_fd, F_SETFL, flags | O_NONBLOCK);
        ack_fd = udp_fd;
        if (ack_port != 5005) {
            fprintf(stderr, "warning: --ack-port is ignored in relay mode\n");
        }
    }
    FrameSendEntry frame_map[FRAME_MAP_SIZE];
    memset(frame_map, 0, sizeof(frame_map));

    enc_frame = av_frame_alloc();
    pkt = av_packet_alloc();
    if (!enc_frame || !pkt) {
        ret = AVERROR(ENOMEM);
        goto end;
    }

    enc_frame->format = AV_PIX_FMT_YUV420P;
    enc_frame->width = width;
    enc_frame->height = height;
    ret = av_frame_get_buffer(enc_frame, 32);
    if (ret < 0) {
        print_fferr("av_frame_get_buffer failed", ret);
        goto end;
    }

    if (!use_fake) {
        src_frame = av_frame_alloc();
        if (!src_frame) {
            ret = AVERROR(ENOMEM);
            goto end;
        }
        src_frame->format = src_pix_fmt;
        src_frame->width = width;
        src_frame->height = height;

        sws = sws_getContext(width, height, src_pix_fmt, width, height, AV_PIX_FMT_YUV420P, SWS_BILINEAR, NULL, NULL, NULL);
        if (!sws) {
            ret = AVERROR(EINVAL);
            goto end;
        }
    }

    int bsf_on = 0;
    if (strcmp(encoder_name, "libx265") == 0) {
        if (probe_encoder_annexb(enc_ctx, enc_frame, pkt)) {
            hevc_bsf = NULL;
            bsf_on = 0;
        } else {
            const AVBitStreamFilter *bsf = av_bsf_get_by_name("hevc_mp4toannexb");
            if (!bsf) {
                fprintf(stderr, "hevc_mp4toannexb bsf not found\n");
                ret = -1;
                goto end;
            }
            ret = av_bsf_alloc(bsf, &hevc_bsf);
            if (ret < 0) {
                print_fferr("av_bsf_alloc failed", ret);
                goto end;
            }
            ret = avcodec_parameters_from_context(hevc_bsf->par_in, enc_ctx);
            if (ret < 0) {
                print_fferr("avcodec_parameters_from_context failed", ret);
                goto end;
            }
            hevc_bsf->time_base_in = enc_ctx->time_base;
            ret = av_bsf_init(hevc_bsf);
            if (ret < 0) {
                print_fferr("av_bsf_init failed", ret);
                goto end;
            }
            bsf_on = 1;
        }
    }

    Stats stats = {
        .last_report_us = av_gettime_relative(),
        .interval_us = (int64_t)(report_sec > 0 ? report_sec : 2) * 1000000,
    };
    rtp_ctx.stats = &stats;

    uint32_t rtp_ts = 0;
    uint32_t rtp_step = RTP_CLOCK / (uint32_t)(fps > 0 ? fps : 30);
    if (rtp_step == 0) rtp_step = 3000;

    printf("sender start mode=%s session=%s dev=%s %dx%d@%dfps encoder=%s bitrate=%" PRId64
           " gop=%d bsf=%s fec=%d k=%d -> %s:%d\n",
           transport_mode_name(mode),
           mode == TRANSPORT_MODE_RELAY ? rtp_ctx.session_id : "-",
           device,
           width,
           height,
           fps,
           encoder_name,
           bit_rate,
           gop_size,
           bsf_on ? "on" : "off",
           fec_sender.enabled,
           fec_sender.k,
           peer_ip,
           peer_port);

    int force_idr = 1;
    uint32_t stats_seq = 0;
    int64_t stats_last_send_us = 0;
    const int64_t encode_interval_us = 1000000 / fps;
    int64_t next_encode_at_us = 0;
    while (!g_stop) {
        int64_t loop_now = av_gettime_relative();
        poll_frame_ack_once(ack_fd, frame_map, &stats, &force_idr, mode, rtp_ctx.session_id, &rtp_ctx);
        maybe_send_stats_report(&rtp_ctx, &stats, loop_now, &stats_seq, &stats_last_send_us);
        struct v4l2_buffer buf;
        CLEAR(buf);
        int have_frame = 0;
        int queue_drop = 0;

        if (use_fake) {
            have_frame = 1;
        } else if (use_libcamera) {
            const uint8_t *y = NULL;
            const uint8_t *uv = NULL;
            int y_stride = 0;
            int uv_stride = 0;
            int lw = 0;
            int lh = 0;
            if (libcamera_capture_wait_frame(lc_cap, &y, &uv, &y_stride, &uv_stride, &lw, &lh, 100) == 0) {
                size_t y_size = (size_t)y_stride * (size_t)lh;
                size_t total = nv12_frame_bytes(y_stride, lh);
                if (ensure_nv12_buf(&src_nv12_buf, &src_nv12_cap, y_stride, lh) == 0) {
                    memcpy(src_nv12_buf, y, total);
                    src_frame->data[0] = src_nv12_buf;
                    src_frame->data[1] = src_nv12_buf + y_size;
                    src_frame->data[2] = NULL;
                    src_frame->linesize[0] = y_stride;
                    src_frame->linesize[1] = uv_stride;
                    have_frame = 1;
                }
            }
        } else if (use_d405) {
            const uint8_t *data = NULL;
            int stride = 0;
            int dw = 0;
            int dh = 0;
            if (d405_capture_wait_frame(d405_cap, &data, &stride, &dw, &dh, 100) == 0) {
                src_frame->data[0] = (uint8_t *)data;
                src_frame->linesize[0] = stride;
                have_frame = 1;
            }
        } else {
            fd_set fds;
            FD_ZERO(&fds);
            FD_SET(v4l2_fd, &fds);
            struct timeval tv = {.tv_sec = 1, .tv_usec = 0};

            int sel = select(v4l2_fd + 1, &fds, NULL, NULL, &tv);
            if (sel < 0) {
                if (errno == EINTR) continue;
                perror("select failed");
                break;
            }
            if (sel == 0) continue;

            int drained = v4l2_drain_to_latest(v4l2_fd, &buf, &queue_drop);
            if (drained < 0) {
                perror("VIDIOC_DQBUF failed");
                break;
            }
            if (drained == 0) continue;
            src_frame->data[0] = (uint8_t *)v4l2_bufs[buf.index].start;
            src_frame->linesize[0] = width * 2;
            have_frame = 1;
        }

        if (!have_frame) continue;
        /*
         * D405 can capture at 90 fps while the transport target is lower.
         * Keep the newest frame and drop surplus captures before encoding,
         * otherwise --fps affects timestamps only and all 90 frames are sent.
         */
        if (use_d405) {
            int64_t now_us = av_gettime_relative();
            if (!encode_cadence_accept(&next_encode_at_us, now_us, encode_interval_us)) continue;
        }
        if (queue_drop > 0) {
            stats.queue_drop_interval += queue_drop;
            if (queue_drop > stats.max_queue_drop_interval) {
                stats.max_queue_drop_interval = queue_drop;
            }
        }

        if ((ret = av_frame_make_writable(enc_frame)) < 0) {
            print_fferr("av_frame_make_writable failed", ret);
            if (!use_fake && !use_libcamera && !use_d405) xioctl(v4l2_fd, VIDIOC_QBUF, &buf);
            break;
        }

        int64_t t0 = av_gettime_relative();
        if (use_fake) {
            fill_fake_yuv420p(enc_frame, fake_frame_idx++);
        } else {
            sws_scale(sws,
                      (const uint8_t *const *)src_frame->data,
                      src_frame->linesize,
                      0,
                      height,
                      enc_frame->data,
                      enc_frame->linesize);
        }
        int64_t t_sws = av_gettime_relative();
        enc_frame->pts += 1;

        if (force_idr) {
            apply_force_idr(enc_ctx, enc_frame);
            force_idr = 0;
        } else {
            clear_force_idr(enc_ctx, enc_frame);
        }

        ret = avcodec_send_frame(enc_ctx, enc_frame);
        if (ret < 0) {
            print_fferr("avcodec_send_frame failed", ret);
            stats.dropped_frames_total++;
            if (!use_fake && !use_libcamera && !use_d405) xioctl(v4l2_fd, VIDIOC_QBUF, &buf);
            continue;
        }

        int got_packet = 0;
        while (1) {
            ret = avcodec_receive_packet(enc_ctx, pkt);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
            if (ret < 0) {
                print_fferr("avcodec_receive_packet failed", ret);
                break;
            }

            got_packet = 1;
            if (hevc_bsf) {
                ret = av_bsf_send_packet(hevc_bsf, pkt);
                if (ret < 0) {
                    print_fferr("av_bsf_send_packet failed", ret);
                    stats.dropped_frames_total++;
                    av_packet_unref(pkt);
                    continue;
                }
                av_packet_unref(pkt);
                while (1) {
                    ret = av_bsf_receive_packet(hevc_bsf, pkt);
                    if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
                    if (ret < 0) {
                        print_fferr("av_bsf_receive_packet failed", ret);
                        stats.dropped_frames_total++;
                        break;
                    }
                    if (rtp_payload_encode_input(rtp_packer, pkt->data, pkt->size, rtp_ts) != 0) {
                        stats.dropped_frames_total++;
                    }
                    frame_map_store(frame_map, rtp_ts, av_gettime_relative());
                    count_h265_annexb_nalus(pkt->data, pkt->size, &stats);
                    av_packet_unref(pkt);
                }
            } else {
                if (rtp_payload_encode_input(rtp_packer, pkt->data, pkt->size, rtp_ts) != 0) {
                    stats.dropped_frames_total++;
                }
                frame_map_store(frame_map, rtp_ts, av_gettime_relative());
                count_h265_annexb_nalus(pkt->data, pkt->size, &stats);
                av_packet_unref(pkt);
            }
        }

        if (got_packet) {
            int64_t t1 = av_gettime_relative();
            stats.encoded_frames_interval++;
            stats.encoded_frames_total++;
            stats.encode_us_interval += (double)(t1 - t0);
            stats.sws_us_interval += (double)(t_sws - t0);
            stats.enc_us_interval += (double)(t1 - t_sws);
        } else {
            stats.dropped_frames_total++;
        }

        rtp_ts += rtp_step;

        if (!use_fake && !use_libcamera && !use_d405) {
            if (xioctl(v4l2_fd, VIDIOC_QBUF, &buf) < 0) {
                perror("VIDIOC_QBUF failed");
                break;
            }
        } else if (use_fake) {
            av_usleep((unsigned int)(1000000 / (fps > 0 ? fps : 30)));
        }

        report_stats_if_needed(&stats, mode, rtp_ctx.session_id, health_file);
    }

end:
    if (ack_fd >= 0) close(ack_fd);
    if (rtp_packer) rtp_payload_encode_destroy(rtp_packer);
    if (udp_fd >= 0) close(udp_fd);
    av_packet_free(&pkt);
    if (hevc_bsf) av_bsf_free(&hevc_bsf);
    av_frame_free(&src_frame);
    av_frame_free(&enc_frame);
    sws_freeContext(sws);
    avcodec_free_context(&enc_ctx);
    if (lc_cap) libcamera_capture_close(lc_cap);
    if (d405_cap) d405_capture_close(d405_cap);
    free(src_nv12_buf);
    deinit_v4l2(v4l2_fd, v4l2_bufs, v4l2_buf_count);
    return ret < 0 ? 1 : 0;
}
