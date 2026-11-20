#ifndef RELAY_PROTO_H
#define RELAY_PROTO_H

#include "platform_net.h"
#include <stdint.h>
#include <string.h>

#define RELAY_HEADER_MAGIC 0x4c545248u
#define RELAY_SESSION_ID_SIZE 36u
#define RELAY_SESSION_ID_STR_SIZE 37u
#define RELAY_DEFAULT_SESSION_ID "default"
#define RELAY_HEADER_SIZE (8u + RELAY_SESSION_ID_SIZE)

enum {
    RELAY_TYPE_RTP = 1,
    RELAY_TYPE_HEARTBEAT = 2,
    RELAY_TYPE_SENDER_STATS = 3,
    /* Relay -> sender low-rate proof that video reached the relay ingress. */
    RELAY_TYPE_INGRESS_ACK = 4,
};

typedef struct {
    uint32_t magic;
    uint16_t header_size;
    uint16_t type;
    char session_id[RELAY_SESSION_ID_STR_SIZE];
} RelayHeader;

static inline size_t relay_bounded_strlen(const char *s, size_t max_len) {
    size_t n = 0;
    if (!s) return 0;
    while (n < max_len && s[n]) n++;
    return n;
}

static inline void relay_session_id_copy(char *dst, const char *src) {
    memset(dst, 0, RELAY_SESSION_ID_STR_SIZE);
    if (!src || !*src) src = RELAY_DEFAULT_SESSION_ID;
    memcpy(dst, src, relay_bounded_strlen(src, RELAY_SESSION_ID_SIZE));
    dst[RELAY_SESSION_ID_SIZE] = '\0';
}

static inline void relay_header_write(uint8_t *dst, uint16_t type, const char *session_id) {
    uint32_t magic = htonl(RELAY_HEADER_MAGIC);
    uint16_t header_size = htons((uint16_t)RELAY_HEADER_SIZE);
    uint16_t type_n = htons(type);
    memset(dst, 0, RELAY_HEADER_SIZE);
    memcpy(dst, &magic, sizeof(magic));
    memcpy(dst + 4, &header_size, sizeof(header_size));
    memcpy(dst + 6, &type_n, sizeof(type_n));
    if (!session_id || !*session_id) session_id = RELAY_DEFAULT_SESSION_ID;
    memcpy(dst + 8, session_id, relay_bounded_strlen(session_id, RELAY_SESSION_ID_SIZE));
}

static inline int relay_header_read(const uint8_t *src, size_t bytes, RelayHeader *out) {
    uint32_t magic;
    uint16_t header_size;
    uint16_t type;
    if (bytes < RELAY_HEADER_SIZE) return 0;
    memcpy(&magic, src, sizeof(magic));
    memcpy(&header_size, src + 4, sizeof(header_size));
    memcpy(&type, src + 6, sizeof(type));
    out->magic = ntohl(magic);
    out->header_size = ntohs(header_size);
    out->type = ntohs(type);
    if (out->magic != RELAY_HEADER_MAGIC || out->header_size != RELAY_HEADER_SIZE) return 0;
    memcpy(out->session_id, src + 8, RELAY_SESSION_ID_SIZE);
    out->session_id[RELAY_SESSION_ID_SIZE] = '\0';
    return 1;
}

#endif
