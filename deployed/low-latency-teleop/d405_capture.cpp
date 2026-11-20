#include "d405_capture.h"

#include <librealsense2/rs.hpp>

#include <cstdio>
#include <cstdlib>
#include <exception>

struct D405Capture {
    rs2::pipeline pipe;
    rs2::pipeline_profile profile;
    rs2::frame frame;
};

static bool parse_env_float(const char *name, float *out) {
    const char *value = std::getenv(name);
    if (!value || !*value) return false;
    char *end = nullptr;
    float parsed = std::strtof(value, &end);
    if (!value[0] || (end && *end)) {
        std::fprintf(stderr, "d405: ignoring invalid %s=%s\n", name, value);
        return false;
    }
    *out = parsed;
    return true;
}

static bool sensor_has_color_stream(const rs2::sensor &sensor) {
    for (const auto &profile : sensor.get_stream_profiles()) {
        if (profile.stream_type() == RS2_STREAM_COLOR) return true;
    }
    return false;
}

static void set_clamped_option(rs2::sensor &sensor, rs2_option option, float value, const char *name) {
    if (!sensor.supports(option)) {
        std::fprintf(stderr, "d405: color sensor does not support %s\n", name);
        return;
    }
    rs2::option_range range = sensor.get_option_range(option);
    float clamped = value;
    if (clamped < range.min) clamped = range.min;
    if (clamped > range.max) clamped = range.max;
    sensor.set_option(option, clamped);
    std::fprintf(stderr, "d405: set %s=%.3f\n", name, clamped);
}

extern "C" D405Capture *d405_capture_open(int width, int height, int fps, const char *serial) {
    try {
        D405Capture *cap = new D405Capture();
        rs2::config cfg;
        if (serial && *serial) {
            cfg.enable_device(serial);
        }
        cfg.enable_stream(RS2_STREAM_COLOR, width, height, RS2_FORMAT_YUYV, fps);
        cap->profile = cap->pipe.start(cfg);

        for (rs2::sensor sensor : cap->profile.get_device().query_sensors()) {
            if (sensor.supports(RS2_OPTION_FRAMES_QUEUE_SIZE)) {
                sensor.set_option(RS2_OPTION_FRAMES_QUEUE_SIZE, 1.0f);
            }
            if (sensor.supports(RS2_OPTION_GLOBAL_TIME_ENABLED)) {
                sensor.set_option(RS2_OPTION_GLOBAL_TIME_ENABLED, 1.0f);
            }
            if (!sensor_has_color_stream(sensor)) continue;

            float exposure_us = 0.0f;
            if (parse_env_float("D405_EXPOSURE_US", &exposure_us) && exposure_us > 0.0f) {
                if (sensor.supports(RS2_OPTION_ENABLE_AUTO_EXPOSURE)) {
                    sensor.set_option(RS2_OPTION_ENABLE_AUTO_EXPOSURE, 0.0f);
                    std::fprintf(stderr, "d405: set auto_exposure=off\n");
                }
                set_clamped_option(sensor, RS2_OPTION_EXPOSURE, exposure_us, "exposure_us");
            }
            float gain = 0.0f;
            if (parse_env_float("D405_GAIN", &gain) && gain >= 0.0f) {
                set_clamped_option(sensor, RS2_OPTION_GAIN, gain, "gain");
            }
        }

        rs2::video_stream_profile color_profile =
            cap->profile.get_stream(RS2_STREAM_COLOR).as<rs2::video_stream_profile>();
        auto dev = cap->profile.get_device();
        const char *name = dev.supports(RS2_CAMERA_INFO_NAME)
                               ? dev.get_info(RS2_CAMERA_INFO_NAME)
                               : "RealSense";
        const char *dev_serial = dev.supports(RS2_CAMERA_INFO_SERIAL_NUMBER)
                                     ? dev.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER)
                                     : "unknown";
        std::fprintf(stderr, "d405 capture %s serial=%s color=%dx%d@%d YUYV low-latency\n",
                     name,
                     dev_serial,
                     color_profile.width(),
                     color_profile.height(),
                     color_profile.fps());
        return cap;
    } catch (const rs2::error &e) {
        std::fprintf(stderr, "d405 RealSense error: %s (%s)\n", e.what(), e.get_failed_function().c_str());
    } catch (const std::exception &e) {
        std::fprintf(stderr, "d405 error: %s\n", e.what());
    }
    return nullptr;
}

extern "C" void d405_capture_close(D405Capture *cap) {
    if (!cap) return;
    try {
        cap->pipe.stop();
    } catch (...) {
    }
    delete cap;
}

extern "C" int d405_capture_wait_frame(D405Capture *cap,
                                        const uint8_t **data,
                                        int *stride,
                                        int *out_width,
                                        int *out_height,
                                        int timeout_ms) {
    if (!cap || !data || !stride || !out_width || !out_height) return -1;
    try {
        rs2::frameset frames;
        if (!cap->pipe.poll_for_frames(&frames)) {
            frames = cap->pipe.wait_for_frames((unsigned int)timeout_ms);
        }
        rs2::frameset newer;
        while (cap->pipe.poll_for_frames(&newer)) {
            frames = newer;
        }
        rs2::video_frame color = frames.get_color_frame();
        if (!color) return -1;
        cap->frame = color;
        *data = static_cast<const uint8_t *>(color.get_data());
        *stride = color.get_stride_in_bytes();
        *out_width = color.get_width();
        *out_height = color.get_height();
        return 0;
    } catch (const std::exception &e) {
        std::fprintf(stderr, "d405 wait frame error: %s\n", e.what());
    }
    return -1;
}
