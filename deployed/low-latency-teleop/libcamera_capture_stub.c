#include "libcamera_capture.h"

#include <stdio.h>

LibcameraCapture *libcamera_capture_open(int width, int height, int fps) {
    (void)width;
    (void)height;
    (void)fps;
    fprintf(stderr, "libcamera backend not built; install libcamera-dev and rebuild with USE_LIBCAMERA=1\n");
    return NULL;
}

void libcamera_capture_close(LibcameraCapture *cap) {
    (void)cap;
}

int libcamera_capture_wait_frame(LibcameraCapture *cap,
                               const uint8_t **y,
                               const uint8_t **uv,
                               int *y_stride,
                               int *uv_stride,
                               int *out_width,
                               int *out_height,
                               int timeout_ms) {
    (void)cap;
    (void)y;
    (void)uv;
    (void)y_stride;
    (void)uv_stride;
    (void)out_width;
    (void)out_height;
    (void)timeout_ms;
    return -1;
}
