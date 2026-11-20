#include "receiver_config.h"

#include <stdio.h>

static int failed;
#define EXPECT(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failed++; } } while (0)

static ReceiverReorderConfig parse(const char *text) {
    ReceiverReorderConfig cfg;
    FILE *file = tmpfile();
    if (!file) {
        fprintf(stderr, "tmpfile failed\n");
        failed++;
        receiver_reorder_config_defaults(&cfg);
        return cfg;
    }
    fputs(text, file);
    rewind(file);
    EXPECT(receiver_reorder_config_parse(file, &cfg) == 0);
    fclose(file);
    return cfg;
}

static void test_valid_values(void) {
    ReceiverReorderConfig cfg = parse("video:\n  receiver:\n    reorder_capacity: 64\n    reorder_wait_ms: 20\n");
    EXPECT(cfg.reorder_capacity == 64);
    EXPECT(cfg.reorder_wait_ms == 20);
    EXPECT(!cfg.capacity_invalid && !cfg.wait_invalid);
}

static void test_defaults_when_missing(void) {
    ReceiverReorderConfig cfg = parse("video:\n  review_rates:\n    - 1.0\n");
    EXPECT(cfg.reorder_capacity == 8);
    EXPECT(cfg.reorder_wait_ms == 10);
}

static void test_invalid_values_fall_back_independently(void) {
    ReceiverReorderConfig cfg = parse("video:\n  receiver:\n    reorder_capacity: 65\n    reorder_wait_ms: -1\n");
    EXPECT(cfg.reorder_capacity == 8 && cfg.capacity_invalid);
    EXPECT(cfg.reorder_wait_ms == 10 && cfg.wait_invalid);
    cfg = parse("video:\n  receiver:\n    reorder_capacity: nope\n    reorder_wait_ms: 21\n");
    EXPECT(cfg.reorder_capacity == 8 && cfg.capacity_invalid);
    EXPECT(cfg.reorder_wait_ms == 10 && cfg.wait_invalid);
}

int main(void) {
    test_valid_values();
    test_defaults_when_missing();
    test_invalid_values_fall_back_independently();
    if (failed) return 1;
    puts("receiver_config_selftest: ok");
    return 0;
}
