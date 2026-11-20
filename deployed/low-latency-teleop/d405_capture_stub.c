#include "d405_capture.h"

#include <stdio.h>

D405Capture *d405_capture_open(int width, int height, int fps, const char *serial) {
    (void)width;
    (void)height;
    (void)fps;
    (void)serial;
    fprintf(stderr, "d405 backend not built; install librealsense2-dev and rebuild\n");
    return NULL;
}

void d405_capture_close(D405Capture *cap) {
    (void)cap;
}

int d405_capture_wait_frame(D405Capture *cap,
                            const uint8_t **data,
                            int *stride,
                            int *out_width,
                            int *out_height,
                            int timeout_ms) {
    (void)cap;
    (void)data;
    (void)stride;
    (void)out_width;
    (void)out_height;
    (void)timeout_ms;
    return -1;
}
