#define _POSIX_C_SOURCE 200112L

#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include "frame_ack.h"
#include "relay_proto.h"

#define MAX_UDP_PACKET 65536
#define MAX_SESSIONS 128
#define DEFAULT_LISTEN_PORT 5004
#define DEFAULT_REPORT_SEC 2
#define RECEIVER_HEARTBEAT_TIMEOUT_US 10000000LL
#define INGRESS_ACK_INTERVAL_US 1000000LL
#define MAX_PENDING_PACKETS 1024
#define QUEUE_ALERT_THRESHOLD 256
#define QUEUE_ALERT_INTERVAL_US 1000000LL

static volatile sig_atomic_t g_stop = 0;

typedef struct {
    int in_use;
    char session_id[RELAY_SESSION_ID_STR_SIZE];
    struct sockaddr_in receiver_addr;
    int has_receiver_addr;
    int64_t last_receiver_heartbeat_us;
    struct sockaddr_in sender_addr;
    int has_sender_addr;
    int64_t rtp_packets;
    int64_t rtp_bytes;
    int64_t ack_packets;
    int64_t ack_bytes;
    int64_t heartbeat_packets;
    int64_t stats_packets;
    int64_t stats_bytes;
    int64_t ingress_ack_packets;
    int64_t last_ingress_ack_us;
    int64_t dropped_packets;
    int64_t send_errors;
} RelaySession;

typedef struct {
    uint8_t data[MAX_UDP_PACKET];
    ssize_t len;
    struct sockaddr_in from;
    int64_t enqueued_us;
} PendingPacket;

typedef struct {
    PendingPacket items[MAX_PENDING_PACKETS];
    int head;
    int tail;
    int size;
    int peak_size;
    int64_t dropped_full;
} PacketQueue;

typedef struct {
    int fd;
    RelaySession sessions[MAX_SESSIONS];
    int64_t last_report_us;
    int64_t report_interval_us;
    PacketQueue arm_queue;
    PacketQueue default_queue;
    int64_t last_queue_alert_us;
    int64_t arm_latency_last_us;
    int64_t arm_latency_sum_us;
    int64_t arm_latency_count;
    int64_t video_latency_last_us;
    int64_t video_latency_sum_us;
    int64_t video_latency_count;
} RelayContext;

static void handle_sigint(int sig) {
    (void)sig;
    g_stop = 1;
}

static int64_t now_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "usage: %s [listen_port]\n"
            "\n"
            "All relay traffic uses one UDP listen_port.\n"
            "sender sends Header+RTP, receiver sends Header+HEARTBEAT.\n"
            "Header+HEARTBEAT with an empty payload is a receiver keepalive.\n"
            "Header+HEARTBEAT with a payload is forwarded to sender as ACK data.\n"
            "Header+SENDER_STATS is forwarded to receiver (net RTT OSD).\n"
            "session_id string is read from the header and used to route each session independently.\n"
            "\n"
            "defaults:\n"
            "  listen_port=%d\n",
            argv0,
            DEFAULT_LISTEN_PORT);
}

static int open_udp_listener(int port) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }

    int reuse = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(fd);
        return -1;
    }

    return fd;
}

static RelaySession *find_session(RelayContext *ctx, const char *session_id, int create) {
    RelaySession *free_slot = NULL;
    for (int i = 0; i < MAX_SESSIONS; i++) {
        RelaySession *s = &ctx->sessions[i];
        if (s->in_use && strcmp(s->session_id, session_id) == 0) return s;
        if (!s->in_use && !free_slot) free_slot = s;
    }
    if (!create || !free_slot) return NULL;

    memset(free_slot, 0, sizeof(*free_slot));
    free_slot->in_use = 1;
    relay_session_id_copy(free_slot->session_id, session_id);
    printf("relay created session=%s\n", free_slot->session_id);
    fflush(stdout);
    return free_slot;
}

static void addr_to_string(const struct sockaddr_in *addr, char *buf, size_t len) {
    char ip[INET_ADDRSTRLEN];
    const char *p = inet_ntop(AF_INET, &addr->sin_addr, ip, sizeof(ip));
    if (!p) {
        snprintf(buf, len, "?:%u", (unsigned)ntohs(addr->sin_port));
        return;
    }
    snprintf(buf, len, "%s:%u", ip, (unsigned)ntohs(addr->sin_port));
}

static int starts_with(const char *s, const char *prefix) {
    size_t n = strlen(prefix);
    return strncmp(s, prefix, n) == 0;
}

static int queue_push(PacketQueue *q, const uint8_t *data, ssize_t len, const struct sockaddr_in *from, int64_t now) {
    if (q->size >= MAX_PENDING_PACKETS) {
        q->dropped_full++;
        return 0;
    }
    PendingPacket *pkt = &q->items[q->tail];
    memcpy(pkt->data, data, (size_t)len);
    pkt->len = len;
    pkt->from = *from;
    pkt->enqueued_us = now;
    q->tail = (q->tail + 1) % MAX_PENDING_PACKETS;
    q->size++;
    if (q->size > q->peak_size) q->peak_size = q->size;
    return 1;
}

static int queue_pop(PacketQueue *q, PendingPacket *out) {
    if (q->size <= 0) return 0;
    *out = q->items[q->head];
    q->head = (q->head + 1) % MAX_PENDING_PACKETS;
    q->size--;
    return 1;
}

static void maybe_report(RelayContext *ctx) {
    int64_t t = now_us();
    if (t - ctx->last_report_us < ctx->report_interval_us) return;

    for (int i = 0; i < MAX_SESSIONS; i++) {
        RelaySession *s = &ctx->sessions[i];
        if (!s->in_use) continue;
        if (s->has_receiver_addr && t - s->last_receiver_heartbeat_us > RECEIVER_HEARTBEAT_TIMEOUT_US) {
            s->has_receiver_addr = 0;
            printf("relay receiver heartbeat timeout: session=%s\n", s->session_id);
        }

        char receiver[64];
        char sender[64];
        if (s->has_receiver_addr) {
            addr_to_string(&s->receiver_addr, receiver, sizeof(receiver));
        } else {
            snprintf(receiver, sizeof(receiver), "none");
        }
        if (s->has_sender_addr) {
            addr_to_string(&s->sender_addr, sender, sizeof(sender));
        } else {
            snprintf(sender, sizeof(sender), "none");
        }

        printf("relay report session=%s rtp_packets=%" PRId64 " rtp_bytes=%" PRId64
               " stats_packets=%" PRId64 " stats_bytes=%" PRId64
               " ack_packets=%" PRId64 " ack_bytes=%" PRId64
               " ingress_ack=%" PRId64
               " heartbeat_packets=%" PRId64 " dropped=%" PRId64 " send_errors=%" PRId64
               " receiver=%s sender=%s\n",
               s->session_id,
               s->rtp_packets,
               s->rtp_bytes,
               s->stats_packets,
               s->stats_bytes,
               s->ack_packets,
               s->ack_bytes,
               s->ingress_ack_packets,
               s->heartbeat_packets,
               s->dropped_packets,
               s->send_errors,
               receiver,
               sender);
        s->rtp_packets = 0;
        s->rtp_bytes = 0;
        s->stats_packets = 0;
        s->stats_bytes = 0;
        s->ack_packets = 0;
        s->ack_bytes = 0;
        s->ingress_ack_packets = 0;
        s->heartbeat_packets = 0;
        s->dropped_packets = 0;
        s->send_errors = 0;
    }
    double arm_latency_ms = ctx->arm_latency_count > 0
                                ? (ctx->arm_latency_sum_us / (double)ctx->arm_latency_count) / 1000.0
                                : 0.0;
    double video_latency_ms = ctx->video_latency_count > 0
                                  ? (ctx->video_latency_sum_us / (double)ctx->video_latency_count) / 1000.0
                                  : 0.0;
    printf("relay queue report arm_q=%d default_q=%d arm_peak=%d default_peak=%d "
           "arm_q_drop=%" PRId64 " default_q_drop=%" PRId64
           " arm_latency_ms_last=%.3f arm_latency_ms_avg=%.3f "
           "video_latency_ms_last=%.3f video_latency_ms_avg=%.3f\n",
           ctx->arm_queue.size,
           ctx->default_queue.size,
           ctx->arm_queue.peak_size,
           ctx->default_queue.peak_size,
           ctx->arm_queue.dropped_full,
           ctx->default_queue.dropped_full,
           ctx->arm_latency_last_us / 1000.0,
           arm_latency_ms,
           ctx->video_latency_last_us / 1000.0,
           video_latency_ms);
    ctx->arm_queue.peak_size = ctx->arm_queue.size;
    ctx->default_queue.peak_size = ctx->default_queue.size;
    ctx->arm_latency_sum_us = 0;
    ctx->arm_latency_count = 0;
    ctx->video_latency_sum_us = 0;
    ctx->video_latency_count = 0;
    fflush(stdout);
    ctx->last_report_us = t;
}

static void learn_sender(RelaySession *s, const struct sockaddr_in *from) {
    if (s->has_sender_addr &&
        s->sender_addr.sin_addr.s_addr == from->sin_addr.s_addr &&
        s->sender_addr.sin_port == from->sin_port) {
        return;
    }

    s->sender_addr = *from;
    s->has_sender_addr = 1;

    char sender[64];
    addr_to_string(&s->sender_addr, sender, sizeof(sender));
    printf("relay learned sender target: session=%s %s\n", s->session_id, sender);
    fflush(stdout);
}

static void learn_receiver(RelaySession *s, const struct sockaddr_in *from, int64_t now) {
    s->last_receiver_heartbeat_us = now;
    if (s->has_receiver_addr &&
        s->receiver_addr.sin_addr.s_addr == from->sin_addr.s_addr &&
        s->receiver_addr.sin_port == from->sin_port) {
        return;
    }

    s->receiver_addr = *from;
    s->has_receiver_addr = 1;

    char receiver[64];
    addr_to_string(&s->receiver_addr, receiver, sizeof(receiver));
    printf("relay learned receiver target: session=%s %s\n", s->session_id, receiver);
    fflush(stdout);
}

static int receiver_target_is_fresh(RelaySession *s, int64_t now) {
    if (!s->has_receiver_addr) return 0;
    if (now - s->last_receiver_heartbeat_us <= RECEIVER_HEARTBEAT_TIMEOUT_US) return 1;
    s->has_receiver_addr = 0;
    printf("relay receiver heartbeat timeout: session=%s\n", s->session_id);
    fflush(stdout);
    return 0;
}

static int forward_packet(RelayContext *ctx,
                          RelaySession *s,
                          const uint8_t *packet,
                          ssize_t packet_len,
                          const struct sockaddr_in *target,
                          int is_ack) {
    ssize_t sent = sendto(ctx->fd,
                          packet,
                          (size_t)packet_len,
                          0,
                          (const struct sockaddr *)target,
                          sizeof(*target));
    if (sent != packet_len) {
        s->send_errors++;
        if (sent < 0) perror(is_ack ? "sendto sender ack" : "sendto receiver");
        return -1;
    }
    return 0;
}

static int forward_payload(RelayContext *ctx,
                           RelaySession *s,
                           const uint8_t *payload,
                           ssize_t payload_len,
                           const struct sockaddr_in *target,
                           int is_ack) {
    ssize_t sent = sendto(ctx->fd,
                          payload,
                          (size_t)payload_len,
                          0,
                          (const struct sockaddr *)target,
                          sizeof(*target));
    if (sent != payload_len) {
        s->send_errors++;
        if (sent < 0) perror(is_ack ? "sendto sender ack" : "sendto receiver");
        return -1;
    }
    return 0;
}

static void maybe_send_ingress_ack(RelayContext *ctx, RelaySession *s, int64_t now) {
    if (!s->has_sender_addr || now - s->last_ingress_ack_us < INGRESS_ACK_INTERVAL_US) return;

    uint8_t packet[RELAY_HEADER_SIZE];
    relay_header_write(packet, RELAY_TYPE_INGRESS_ACK, s->session_id);
    if (forward_packet(ctx, s, packet, (ssize_t)sizeof(packet), &s->sender_addr, 1) == 0) {
        s->last_ingress_ack_us = now;
        s->ingress_ack_packets++;
    }
}

static void process_packet(RelayContext *ctx, const PendingPacket *pkt, int is_arm_queue) {
    const uint8_t *buf = pkt->data;
    ssize_t n = pkt->len;
    const struct sockaddr_in *from = &pkt->from;
    int64_t now = now_us();
    int64_t q_latency_us = now - pkt->enqueued_us;
    if (q_latency_us < 0) q_latency_us = 0;
    if (is_arm_queue) {
        ctx->arm_latency_last_us = q_latency_us;
        ctx->arm_latency_sum_us += q_latency_us;
        ctx->arm_latency_count++;
    } else {
        ctx->video_latency_last_us = q_latency_us;
        ctx->video_latency_sum_us += q_latency_us;
        ctx->video_latency_count++;
    }

    RelayHeader hdr;
    if (!relay_header_read(buf, (size_t)n, &hdr)) return;

    RelaySession *s = find_session(ctx, hdr.session_id, 1);
    if (!s) {
        fprintf(stderr, "drop packet: session table full\n");
        return;
    }

    const uint8_t *payload = buf + RELAY_HEADER_SIZE;
    ssize_t payload_len = n - RELAY_HEADER_SIZE;
    if (hdr.type == RELAY_TYPE_RTP) {
        learn_sender(s, from);
        maybe_send_ingress_ack(ctx, s, now);
        if (!receiver_target_is_fresh(s, now)) {
            s->dropped_packets++;
            return;
        }
        if (forward_payload(ctx, s, payload, payload_len, &s->receiver_addr, 0) == 0) {
            s->rtp_packets++;
            s->rtp_bytes += payload_len;
        }
        return;
    }

    if (hdr.type == RELAY_TYPE_HEARTBEAT) {
        learn_receiver(s, from, now);
        if (payload_len == 0) {
            s->heartbeat_packets++;
            return;
        }
        if (!s->has_sender_addr) {
            s->dropped_packets++;
            return;
        }
        if (forward_payload(ctx, s, payload, payload_len, &s->sender_addr, 1) == 0) {
            s->ack_packets++;
            s->ack_bytes += payload_len;
        }
        return;
    }

    if (hdr.type == RELAY_TYPE_SENDER_STATS) {
        learn_sender(s, from);
        if (!receiver_target_is_fresh(s, now)) {
            s->dropped_packets++;
            return;
        }
        if (payload_len != (ssize_t)sizeof(FrameAckStats)) {
            s->dropped_packets++;
            return;
        }
        /* Keep relay header so receiver can match session/type (RTP strips header). */
        if (forward_packet(ctx, s, buf, n, &s->receiver_addr, 0) == 0) {
            s->stats_packets++;
            s->stats_bytes += n;
        }
        return;
    }

    s->dropped_packets++;
}

static void enqueue_incoming_packet(RelayContext *ctx) {
    uint8_t buf[MAX_UDP_PACKET];
    struct sockaddr_in from;
    socklen_t from_len = sizeof(from);
    ssize_t n = recvfrom(ctx->fd, buf, sizeof(buf), 0, (struct sockaddr *)&from, &from_len);
    if (n < 0) {
        if (errno != EINTR) perror("recvfrom");
        return;
    }

    RelayHeader hdr;
    if (!relay_header_read(buf, (size_t)n, &hdr)) return;

    PacketQueue *target = starts_with(hdr.session_id, "arm") ? &ctx->arm_queue : &ctx->default_queue;
    int64_t now = now_us();
    if (!queue_push(target, buf, n, &from, now)) {
        if (now - ctx->last_queue_alert_us >= QUEUE_ALERT_INTERVAL_US) {
            printf("relay queue alert arm_q=%d default_q=%d arm_q_drop=%" PRId64
                   " default_q_drop=%" PRId64 "\n",
                   ctx->arm_queue.size,
                   ctx->default_queue.size,
                   ctx->arm_queue.dropped_full,
                   ctx->default_queue.dropped_full);
            fflush(stdout);
            ctx->last_queue_alert_us = now;
        }
    }
}

static void drain_queues(RelayContext *ctx) {
    PendingPacket pkt;
    /* Arm first (low latency), then drain all video.
     * Previously only 1 video packet per cycle was forwarded, which let
     * default_q grow to hundreds of packets and multi-second video delay
     * when arm+video shared one relay. */
    while (queue_pop(&ctx->arm_queue, &pkt)) {
        process_packet(ctx, &pkt, 1);
    }
    while (queue_pop(&ctx->default_queue, &pkt)) {
        process_packet(ctx, &pkt, 0);
    }
}

static void maybe_queue_alert(RelayContext *ctx) {
    int64_t now = now_us();
    if (ctx->arm_queue.size < QUEUE_ALERT_THRESHOLD && ctx->default_queue.size < QUEUE_ALERT_THRESHOLD) return;
    if (now - ctx->last_queue_alert_us < QUEUE_ALERT_INTERVAL_US) return;
    printf("relay queue alert arm_q=%d default_q=%d arm_q_drop=%" PRId64
           " default_q_drop=%" PRId64 "\n",
           ctx->arm_queue.size,
           ctx->default_queue.size,
           ctx->arm_queue.dropped_full,
           ctx->default_queue.dropped_full);
    fflush(stdout);
    ctx->last_queue_alert_us = now;
}

int main(int argc, char **argv) {
    int listen_port = DEFAULT_LISTEN_PORT;

    if (argc > 2) {
        usage(argv[0]);
        return 1;
    }
    if (argc >= 2) listen_port = atoi(argv[1]);

    if (listen_port <= 0 || listen_port > 65535) {
        fprintf(stderr, "ports must be in 1..65535\n");
        return 1;
    }

    /* Packet queues are large (~128MB); keep RelayContext on the heap. */
    RelayContext *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) {
        fprintf(stderr, "relay: failed to allocate context (%zu bytes)\n", sizeof(*ctx));
        return 1;
    }
    ctx->report_interval_us = (int64_t)DEFAULT_REPORT_SEC * 1000000;
    ctx->last_report_us = now_us();

    signal(SIGINT, handle_sigint);

    ctx->fd = open_udp_listener(listen_port);
    if (ctx->fd < 0) {
        free(ctx);
        return 1;
    }

    printf("relay start udp :%d max_sessions=%d\n", listen_port, MAX_SESSIONS);
    fflush(stdout);

    while (!g_stop) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(ctx->fd, &rfds);
        struct timeval tv = {.tv_sec = 0, .tv_usec = 100000};

        int ret = select(ctx->fd + 1, &rfds, NULL, NULL, &tv);
        if (ret < 0) {
            if (errno == EINTR) continue;
            perror("select");
            break;
        }
        if (ret > 0 && FD_ISSET(ctx->fd, &rfds)) {
            enqueue_incoming_packet(ctx);
            while (1) {
                fd_set more;
                struct timeval zero = {.tv_sec = 0, .tv_usec = 0};
                FD_ZERO(&more);
                FD_SET(ctx->fd, &more);
                int more_ret = select(ctx->fd + 1, &more, NULL, NULL, &zero);
                if (more_ret <= 0 || !FD_ISSET(ctx->fd, &more)) break;
                enqueue_incoming_packet(ctx);
            }
        }
        drain_queues(ctx);
        maybe_queue_alert(ctx);
        maybe_report(ctx);
    }

    close(ctx->fd);
    free(ctx);
    return 0;
}
