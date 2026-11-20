#include "libcamera_capture.h"

#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <linux/dma-buf.h>

#include <libcamera/camera.h>
#include <libcamera/camera_manager.h>
#include <libcamera/control_ids.h>
#include <libcamera/formats.h>
#include <libcamera/framebuffer_allocator.h>
#include <libcamera/request.h>
#include <libcamera/stream.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <vector>

using namespace libcamera;

struct LibcameraCapture;
static void request_complete(Request *request, LibcameraCapture *cap);

class CaptureHandler {
public:
    explicit CaptureHandler(LibcameraCapture *owner) : cap_(owner) {}
    void onRequestComplete(Request *request) { request_complete(request, cap_); }

private:
    LibcameraCapture *cap_;
};

struct LibcameraCapture {
    static constexpr int kNumSlots = 2;

    std::unique_ptr<CameraManager> cm;
    std::shared_ptr<Camera> camera;
    Stream *stream = nullptr;
    std::unique_ptr<FrameBufferAllocator> allocator;
    std::vector<std::unique_ptr<Request>> requests;
    std::unique_ptr<CaptureHandler> handler;

    std::mutex mu;
    std::condition_variable cv;
    bool frame_ready = false;
    bool stopped = false;

    std::vector<uint8_t> slot_buf[kNumSlots];
    int ready_slot = -1;
    int held_slot = -1;
    int width = 0;
    int height = 0;
    int stream_stride = 0;
    int fps = 30;
};

static int pick_write_slot(LibcameraCapture *cap) {
    if (cap->held_slot < 0) return cap->ready_slot < 0 ? 0 : (1 - cap->ready_slot);
    return 1 - cap->held_slot;
}

static void requeue_request(Request *request, LibcameraCapture *cap) {
    request->reuse(Request::ReuseBuffers);
    cap->camera->queueRequest(request);
}

static void apply_frame_duration(Request *request, int fps) {
    if (fps <= 0) fps = 30;
    int64_t frame_us = (1000000LL + fps - 1) / fps;
    request->controls().set(controls::FrameDurationLimits,
                            Span<const int64_t, 2>({frame_us, frame_us}));
}

static bool dma_buf_begin_cpu_read(int fd) {
    struct dma_buf_sync sync {};
    sync.flags = DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ;
    return ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync) == 0;
}

static void dma_buf_end_cpu_read(int fd) {
    struct dma_buf_sync sync {};
    sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
    (void)ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync);
}

static void copy_plane_rows(uint8_t *dst,
                            int dst_pitch,
                            const uint8_t *src,
                            int src_pitch,
                            int row_bytes,
                            int rows) {
    if (!dst || !src || dst_pitch <= 0 || src_pitch <= 0 || row_bytes <= 0 || rows <= 0) return;
    for (int row = 0; row < rows; row++) {
        std::memcpy(dst + static_cast<size_t>(row) * static_cast<size_t>(dst_pitch),
                    src + static_cast<size_t>(row) * static_cast<size_t>(src_pitch),
                    static_cast<size_t>(row_bytes));
    }
}

static int env_flag_enabled(const char *name) {
    const char *v = std::getenv(name);
    return v && v[0] == '1' && v[1] == '\0';
}

static void log_plane_layout_once(LibcameraCapture *cap,
                                  int src_y_pitch,
                                  int src_uv_pitch,
                                  const FrameBuffer::Plane &y_plane,
                                  const FrameBuffer::Plane *uv_plane) {
    static bool logged = false;
    if (logged || !env_flag_enabled("LIBCAMERA_DEBUG_PLANES")) return;
    logged = true;
    fprintf(stderr,
            "libcamera planes: %dx%d sc.stride=%d src_y_pitch=%d src_uv_pitch=%d "
            "plane0 off=%u len=%u",
            cap->width,
            cap->height,
            cap->stream_stride,
            src_y_pitch,
            src_uv_pitch,
            y_plane.offset,
            y_plane.length);
    if (uv_plane) {
        fprintf(stderr, " plane1 off=%u len=%u", uv_plane->offset, uv_plane->length);
    }
    fputc('\n', stderr);
}

static bool copy_nv12_from_buffer(const FrameBuffer *buffer, LibcameraCapture *cap) {
    const auto &planes = buffer->planes();
    if (planes.empty()) return false;

    int w = cap->width;
    int h = cap->height;
    if (w <= 0 || h <= 0 || (h % 2) != 0) return false;

    int src_y_pitch = static_cast<int>(planes[0].length / static_cast<unsigned int>(h));
    if (src_y_pitch < w) {
        fprintf(stderr, "libcamera: Y plane pitch %d < width %d (len=%u h=%d)\n",
                src_y_pitch, w, planes[0].length, h);
        return false;
    }

    int src_uv_pitch = src_y_pitch;
    if (planes.size() > 1) {
        src_uv_pitch = static_cast<int>(planes[1].length / static_cast<unsigned int>(h / 2));
        if (src_uv_pitch < w) {
            fprintf(stderr, "libcamera: UV plane pitch %d < width %d (len=%u h=%d)\n",
                    src_uv_pitch, w, planes[1].length, h);
            return false;
        }
    }

    int dst_stride = cap->stream_stride > 0 ? cap->stream_stride : src_y_pitch;
    dst_stride = std::max(dst_stride, std::max(w, src_y_pitch));

    size_t y_size = static_cast<size_t>(dst_stride) * static_cast<size_t>(h);
    size_t uv_size = y_size / 2;
    size_t total = y_size + uv_size;

    const uint8_t *y_src = nullptr;
    const uint8_t *uv_src = nullptr;
    void *map_base = MAP_FAILED;
    size_t map_len = 0;
    void *y_map = MAP_FAILED;
    size_t y_map_len = 0;
    void *uv_map = MAP_FAILED;
    size_t uv_map_len = 0;

    bool same_fd = true;
    const int fd = planes[0].fd.get();
    for (const auto &p : planes) {
        if (p.fd.get() != fd) {
            same_fd = false;
            break;
        }
    }

    if (same_fd) {
        /* Pi NV12: Y and UV share one dmabuf at different offsets — map it once. */
        for (const auto &p : planes) {
            size_t end = static_cast<size_t>(p.offset) + static_cast<size_t>(p.length);
            if (end > map_len) map_len = end;
        }
        map_base = mmap(nullptr, map_len, PROT_READ, MAP_SHARED, fd, 0);
        if (map_base == MAP_FAILED) return false;

        const uint8_t *base = static_cast<const uint8_t *>(map_base);
        y_src = base + planes[0].offset;
        if (planes.size() > 1) {
            uv_src = base + planes[1].offset;
        } else {
            size_t y_bytes = static_cast<size_t>(src_y_pitch) * static_cast<size_t>(h);
            if (planes[0].offset + y_bytes + y_bytes / 2 <= map_len) {
                uv_src = y_src + y_bytes;
            }
        }
    } else {
        /* Separate dmabuf per plane (NV12M). */
        y_map = mmap(nullptr, planes[0].length, PROT_READ, MAP_SHARED, planes[0].fd.get(),
                     planes[0].offset);
        if (y_map == MAP_FAILED) return false;
        y_map_len = planes[0].length;
        y_src = static_cast<const uint8_t *>(y_map);

        if (planes.size() > 1) {
            uv_map = mmap(nullptr, planes[1].length, PROT_READ, MAP_SHARED, planes[1].fd.get(),
                          planes[1].offset);
            if (uv_map == MAP_FAILED) {
                munmap(y_map, y_map_len);
                return false;
            }
            uv_map_len = planes[1].length;
            uv_src = static_cast<const uint8_t *>(uv_map);
        } else {
            size_t y_bytes = static_cast<size_t>(src_y_pitch) * static_cast<size_t>(h);
            if (planes[0].length >= y_bytes + y_bytes / 2) {
                uv_src = y_src + y_bytes;
            }
        }
    }

    if (!y_src || !uv_src) {
        if (map_base != MAP_FAILED) munmap(map_base, map_len);
        if (y_map != MAP_FAILED) munmap(y_map, y_map_len);
        if (uv_map != MAP_FAILED) munmap(uv_map, uv_map_len);
        return false;
    }

    log_plane_layout_once(cap,
                          src_y_pitch,
                          src_uv_pitch,
                          planes[0],
                          planes.size() > 1 ? &planes[1] : nullptr);

    {
        std::lock_guard<std::mutex> lock(cap->mu);
        int slot = pick_write_slot(cap);
        if (cap->slot_buf[slot].size() < total) cap->slot_buf[slot].resize(total);
        uint8_t *dst = cap->slot_buf[slot].data();
        copy_plane_rows(dst, dst_stride, y_src, src_y_pitch, w, h);
        copy_plane_rows(dst + y_size, dst_stride, uv_src, src_uv_pitch, w, h / 2);
        cap->stream_stride = dst_stride;
        cap->ready_slot = slot;
        cap->frame_ready = true;

        if (env_flag_enabled("LIBCAMERA_DUMP_NV12")) {
            static bool dumped = false;
            if (!dumped) {
                dumped = true;
                FILE *fp = std::fopen("/tmp/libcamera_frame.nv12", "wb");
                if (fp) {
                    std::fwrite(dst, 1, total, fp);
                    std::fclose(fp);
                    fprintf(stderr, "libcamera: dumped NV12 to /tmp/libcamera_frame.nv12 (%zu bytes)\n",
                            total);
                }
            }
        }
    }

    if (map_base != MAP_FAILED) munmap(map_base, map_len);
    if (y_map != MAP_FAILED) munmap(y_map, y_map_len);
    if (uv_map != MAP_FAILED) munmap(uv_map, uv_map_len);
    return true;
}

static void request_complete(Request *request, LibcameraCapture *cap) {
    if (request->status() == Request::RequestCancelled) return;
    if (!cap->camera) return;

    const Request::BufferMap &bufs = request->buffers();
    auto it = bufs.find(cap->stream);
    if (it != bufs.end()) {
        const FrameBuffer *buffer = it->second;
        const FrameMetadata &meta = buffer->metadata();

        if (meta.status == FrameMetadata::FrameSuccess && !buffer->planes().empty()) {
            int sync_fd = buffer->planes()[0].fd.get();
            if (dma_buf_begin_cpu_read(sync_fd)) {
                if (copy_nv12_from_buffer(buffer, cap)) cap->cv.notify_one();
                dma_buf_end_cpu_read(sync_fd);
            }
        }
    }

    requeue_request(request, cap);
}

extern "C" LibcameraCapture *libcamera_capture_open(int width, int height, int fps) {
    if (fps <= 0) fps = 30;
    auto cap = new LibcameraCapture();
    cap->width = width;
    cap->height = height;
    cap->fps = fps;

    cap->cm = std::make_unique<CameraManager>();
    if (cap->cm->start()) {
        delete cap;
        return nullptr;
    }

    if (cap->cm->cameras().empty()) {
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    cap->camera = cap->cm->cameras()[0];
    if (cap->camera->acquire()) {
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    std::unique_ptr<CameraConfiguration> cfg =
        cap->camera->generateConfiguration({StreamRole::Viewfinder});
    if (!cfg) {
        cap->camera->release();
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    StreamConfiguration &sc = cfg->at(0);
    sc.size = {static_cast<unsigned int>(width), static_cast<unsigned int>(height)};
    sc.pixelFormat = formats::NV12;
    sc.bufferCount = 2;

    if (cfg->validate() == CameraConfiguration::Invalid) {
        cap->camera->release();
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    cap->stream_stride = static_cast<int>(sc.stride);

    if (cap->camera->configure(cfg.get()) < 0) {
        cap->camera->release();
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    cap->stream = sc.stream();
    cap->width = static_cast<int>(sc.size.width);
    cap->height = static_cast<int>(sc.size.height);

    cap->allocator = std::make_unique<FrameBufferAllocator>(cap->camera);
    for (StreamConfiguration &s : *cfg) {
        if (cap->allocator->allocate(s.stream()) < 0) {
            cap->camera->release();
            cap->cm->stop();
            delete cap;
            return nullptr;
        }
    }

    const std::vector<std::unique_ptr<FrameBuffer>> &buffers = cap->allocator->buffers(cap->stream);
    for (unsigned int i = 0; i < buffers.size(); ++i) {
        std::unique_ptr<Request> req = cap->camera->createRequest();
        if (!req) continue;
        if (req->addBuffer(cap->stream, buffers[i].get()) < 0) continue;
        apply_frame_duration(req.get(), cap->fps);
        cap->requests.push_back(std::move(req));
    }
    if (cap->requests.empty()) {
        cap->camera->release();
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    cap->handler = std::make_unique<CaptureHandler>(cap);
    cap->camera->requestCompleted.connect(cap->handler.get(), &CaptureHandler::onRequestComplete);

    if (cap->camera->start()) {
        cap->camera->release();
        cap->cm->stop();
        delete cap;
        return nullptr;
    }

    for (auto &req : cap->requests) {
        cap->camera->queueRequest(req.get());
    }

    fprintf(stderr, "libcamera capture %dx%d NV12 (Viewfinder @ %dfps)\n", cap->width, cap->height,
            cap->fps);
    return cap;
}

extern "C" void libcamera_capture_close(LibcameraCapture *cap) {
    if (!cap) return;
    {
        std::lock_guard<std::mutex> lock(cap->mu);
        cap->stopped = true;
    }
    cap->cv.notify_all();
    if (cap->camera) {
        cap->camera->requestCompleted.disconnect(cap->handler.get(), &CaptureHandler::onRequestComplete);
        cap->camera->stop();
        cap->camera->release();
        cap->camera.reset();
    }
    if (cap->cm) cap->cm->stop();
    delete cap;
}

extern "C" int libcamera_capture_wait_frame(LibcameraCapture *cap,
                                            const uint8_t **y,
                                            const uint8_t **uv,
                                            int *y_stride,
                                            int *uv_stride,
                                            int *out_width,
                                            int *out_height,
                                            int timeout_ms) {
    if (!cap) return -1;
    std::unique_lock<std::mutex> lock(cap->mu);
    if (!cap->cv.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] {
            return cap->frame_ready || cap->stopped;
        })) {
        return -1;
    }
    if (cap->stopped) return -1;

    int slot = cap->ready_slot;
    cap->ready_slot = -1;
    cap->frame_ready = false;
    cap->held_slot = slot;

    int stride = cap->stream_stride > 0 ? cap->stream_stride : cap->width;
    size_t y_size = static_cast<size_t>(stride) * static_cast<size_t>(cap->height);
    *y = cap->slot_buf[slot].data();
    *uv = cap->slot_buf[slot].data() + y_size;
    *y_stride = stride;
    *uv_stride = stride;
    *out_width = cap->width;
    *out_height = cap->height;
    return 0;
}
