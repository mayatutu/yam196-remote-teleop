#ifndef D405_CAPTURE_H
#define D405_CAPTURE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct D405Capture D405Capture;

D405Capture *d405_capture_open(int width, int height, int fps, const char *serial);
void d405_capture_close(D405Capture *cap);

int d405_capture_wait_frame(D405Capture *cap,
                            const uint8_t **data,
                            int *stride,
                            int *out_width,
                            int *out_height,
                            int timeout_ms);

#ifdef __cplusplus
}
#endif

#endif
