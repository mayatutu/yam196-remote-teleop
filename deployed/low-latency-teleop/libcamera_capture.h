#ifndef LIBCAMERA_CAPTURE_H
#define LIBCAMERA_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct LibcameraCapture LibcameraCapture;

/* Open first camera; request NV12 at width x height. fps is a hint for logging. */
LibcameraCapture *libcamera_capture_open(int width, int height, int fps);
void libcamera_capture_close(LibcameraCapture *cap);

/*
 * Wait up to timeout_ms for a frame. Returns 0 on success.
 * On success, *y and *uv point into an internal buffer valid until the next
 * successful wait or close.
 */
int libcamera_capture_wait_frame(LibcameraCapture *cap,
                                 const uint8_t **y,
                                 const uint8_t **uv,
                                 int *y_stride,
                                 int *uv_stride,
                                 int *out_width,
                                 int *out_height,
                                 int timeout_ms);

#ifdef __cplusplus
}
#endif

#endif
