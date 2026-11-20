#ifndef RECEIVER_CONFIG_H
#define RECEIVER_CONFIG_H

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RECEIVER_REORDER_CAPACITY_DEFAULT 8
#define RECEIVER_REORDER_CAPACITY_MAX 64
#define RECEIVER_REORDER_WAIT_MS_DEFAULT 10
#define RECEIVER_REORDER_WAIT_MS_MAX 20

typedef struct {
    int reorder_capacity;
    int reorder_wait_ms;
    int capacity_invalid;
    int wait_invalid;
} ReceiverReorderConfig;

static inline void receiver_reorder_config_defaults(ReceiverReorderConfig *cfg) {
    if (!cfg) return;
    cfg->reorder_capacity = RECEIVER_REORDER_CAPACITY_DEFAULT;
    cfg->reorder_wait_ms = RECEIVER_REORDER_WAIT_MS_DEFAULT;
    cfg->capacity_invalid = 0;
    cfg->wait_invalid = 0;
}

static inline char *receiver_config_trim(char *text) {
    while (*text && isspace((unsigned char)*text)) text++;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) *--end = '\0';
    return text;
}

static inline int receiver_config_indent(const char *text) {
    int indent = 0;
    while (*text == ' ') {
        indent++;
        text++;
    }
    return indent;
}

static inline int receiver_config_parse_int(const char *text, int min_value, int max_value, int *out) {
    char value[64];
    size_t len = strlen(text);
    if (len == 0 || len >= sizeof(value)) return 0;
    memcpy(value, text, len + 1);
    char *trimmed = receiver_config_trim(value);
    len = strlen(trimmed);
    if (len >= 2 && ((trimmed[0] == '"' && trimmed[len - 1] == '"') ||
                     (trimmed[0] == '\'' && trimmed[len - 1] == '\''))) {
        trimmed[len - 1] = '\0';
        trimmed++;
    }
    errno = 0;
    char *end = NULL;
    long parsed = strtol(trimmed, &end, 10);
    if (errno != 0 || end == trimmed || *receiver_config_trim(end) != '\0' ||
        parsed < min_value || parsed > max_value) {
        return 0;
    }
    *out = (int)parsed;
    return 1;
}

/* Parse only video.receiver scalar settings; the Operator remains the YAML owner. */
static inline int receiver_reorder_config_parse(FILE *file, ReceiverReorderConfig *cfg) {
    if (!file || !cfg) return -1;
    receiver_reorder_config_defaults(cfg);

    char line[512];
    int video_indent = -1;
    int receiver_indent = -1;
    while (fgets(line, sizeof(line), file)) {
        char *comment = strchr(line, '#');
        if (comment) *comment = '\0';
        char *trimmed = receiver_config_trim(line);
        if (!*trimmed) continue;
        int indent = receiver_config_indent(line);

        if (video_indent < 0) {
            if (strcmp(trimmed, "video:") == 0) video_indent = indent;
            continue;
        }
        if (receiver_indent < 0) {
            if (indent <= video_indent) break;
            if (strcmp(trimmed, "receiver:") == 0) receiver_indent = indent;
            continue;
        }
        if (indent <= receiver_indent) break;

        char *separator = strchr(trimmed, ':');
        if (!separator) continue;
        *separator = '\0';
        char *key = receiver_config_trim(trimmed);
        char *value = receiver_config_trim(separator + 1);
        int parsed = 0;
        if (strcmp(key, "reorder_capacity") == 0) {
            if (receiver_config_parse_int(value, 1, RECEIVER_REORDER_CAPACITY_MAX, &parsed)) {
                cfg->reorder_capacity = parsed;
            } else {
                cfg->capacity_invalid = 1;
            }
        } else if (strcmp(key, "reorder_wait_ms") == 0) {
            if (receiver_config_parse_int(value, 0, RECEIVER_REORDER_WAIT_MS_MAX, &parsed)) {
                cfg->reorder_wait_ms = parsed;
            } else {
                cfg->wait_invalid = 1;
            }
        }
    }
    return ferror(file) ? -1 : 0;
}

/* Returns 1 when parsed, 0 when missing, and -1 when unreadable or invalid I/O. */
static inline int receiver_reorder_config_load(const char *path, ReceiverReorderConfig *cfg) {
    receiver_reorder_config_defaults(cfg);
    if (!path || !*path) return 0;
    FILE *file = fopen(path, "r");
    if (!file) return errno == ENOENT ? 0 : -1;
    int result = receiver_reorder_config_parse(file, cfg);
    fclose(file);
    return result == 0 ? 1 : -1;
}

#endif /* RECEIVER_CONFIG_H */
