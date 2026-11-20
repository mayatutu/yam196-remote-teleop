CC ?= gcc
CXX ?= g++
CFLAGS ?= -O2 -Wall -Wextra -std=c11
CXXFLAGS ?= -O2 -Wall -Wextra -std=c++17

FF_PKGS = libavcodec libavutil libswscale
SDL_PKGS = sdl2
SDL_TTF_PKGS = SDL2_ttf

FF_CFLAGS = $(shell pkg-config --cflags $(FF_PKGS))
FF_LIBS = $(shell pkg-config --libs $(FF_PKGS))
SDL_CFLAGS = $(shell pkg-config --cflags $(SDL_PKGS))
SDL_LIBS = $(shell pkg-config --libs $(SDL_PKGS))
SDL_TTF_CFLAGS = $(shell pkg-config --cflags $(SDL_TTF_PKGS))
SDL_TTF_LIBS = $(shell pkg-config --libs $(SDL_TTF_PKGS))
RECEIVER_CFLAGS = $(FF_CFLAGS) $(SDL_CFLAGS) $(SDL_TTF_CFLAGS)
RECEIVER_LIBS = $(FF_LIBS) $(SDL_LIBS) $(SDL_TTF_LIBS)
UNAME_S := $(shell uname -s)
ifneq (,$(findstring MINGW,$(UNAME_S)))
RECEIVER_CFLAGS += -Wno-pointer-sign -Wno-incompatible-pointer-types
RECEIVER_LIBS += -lws2_32
endif
ifneq (,$(findstring MSYS,$(UNAME_S)))
RECEIVER_CFLAGS += -Wno-pointer-sign -Wno-incompatible-pointer-types
RECEIVER_LIBS += -lws2_32
endif

RTP_DIR = deps/media-server/librtp
RTP_INCLUDE = -I$(RTP_DIR)/include
RTP_LOCAL_LIB = $(RTP_DIR)/librtp.a

SENDER = sender
RECEIVER = receiver
RELAY = relay
FEC_SELFTEST = fec_selftest
RTP_REORDER_SELFTEST = rtp_reorder_selftest
ENCODE_CADENCE_SELFTEST = encode_cadence_selftest
RTP_LOSS_RECOVERY_SELFTEST = rtp_loss_recovery_selftest
RECEIVER_CONFIG_SELFTEST = receiver_config_selftest
SCALE_COVER_SELFTEST = scale_cover_selftest

USE_LIBCAMERA ?= 0
ifneq ($(USE_LIBCAMERA),0)
  LIBCAMERA_CFLAGS := $(shell pkg-config --cflags libcamera 2>/dev/null)
  LIBCAMERA_LIBS := $(shell pkg-config --libs libcamera 2>/dev/null)
  ifneq ($(LIBCAMERA_CFLAGS),)
    SENDER_EXTRA_CFLAGS += -DHAVE_LIBCAMERA $(LIBCAMERA_CFLAGS)
    SENDER_EXTRA_OBJ = libcamera_capture.o
    SENDER_EXTRA_LIBS = $(LIBCAMERA_LIBS) -lstdc++
  else
    $(warning USE_LIBCAMERA=1 but libcamera pkg-config not found; using stub)
    SENDER_EXTRA_OBJ = libcamera_capture_stub.o
  endif
else
  SENDER_EXTRA_OBJ = libcamera_capture_stub.o
endif

REALSENSE_CFLAGS := $(shell pkg-config --cflags realsense2 2>/dev/null)
REALSENSE_LIBS := $(shell pkg-config --libs realsense2 2>/dev/null)
ifneq ($(REALSENSE_CFLAGS),)
  SENDER_EXTRA_CFLAGS += -DHAVE_REALSENSE2 $(REALSENSE_CFLAGS)
  SENDER_EXTRA_OBJ += d405_capture.o
  SENDER_EXTRA_LIBS += $(REALSENSE_LIBS) -lstdc++
else
  $(error RealSense SDK development files are required for this deployment)
endif

all: $(SENDER) $(RECEIVER) $(RELAY)

$(RTP_LOCAL_LIB):
	$(MAKE) -C $(RTP_DIR)
	cp $$(find $(RTP_DIR) -name librtp.a | head -n 1) $(RTP_LOCAL_LIB)

$(SENDER): sender.c fec.c fec.h frame_ack.h relay_proto.h $(SENDER_EXTRA_OBJ) $(RTP_LOCAL_LIB)
	$(CC) $(CFLAGS) $(SENDER_EXTRA_CFLAGS) $(FF_CFLAGS) $(RTP_INCLUDE) -o $@ sender.c fec.c $(SENDER_EXTRA_OBJ) $(RTP_LOCAL_LIB) $(FF_LIBS) $(SENDER_EXTRA_LIBS) -pthread

libcamera_capture.o: libcamera_capture.cpp libcamera_capture.h
	$(CXX) $(CXXFLAGS) $(SENDER_EXTRA_CFLAGS) $(LIBCAMERA_CFLAGS) -c -o $@ libcamera_capture.cpp

libcamera_capture_stub.o: libcamera_capture_stub.c libcamera_capture.h
	$(CC) $(CFLAGS) -c -o $@ libcamera_capture_stub.c

d405_capture.o: d405_capture.cpp d405_capture.h
	$(CXX) $(CXXFLAGS) $(SENDER_EXTRA_CFLAGS) $(REALSENSE_CFLAGS) -c -o $@ d405_capture.cpp

d405_capture_stub.o: d405_capture_stub.c d405_capture.h
	$(CC) $(CFLAGS) -c -o $@ d405_capture_stub.c

$(RECEIVER): receiver.c fec.c fec.h frame_ack.h relay_proto.h receiver_config.h rtp_loss_recovery.h rtp_reorder.h scale_cover.h $(RTP_LOCAL_LIB)
	$(CC) $(CFLAGS) $(RECEIVER_CFLAGS) $(RTP_INCLUDE) -o $@ receiver.c fec.c $(RTP_LOCAL_LIB) $(RECEIVER_LIBS)

$(RELAY): relay.c relay_proto.h frame_ack.h
	$(CC) $(CFLAGS) -o $@ relay.c

$(FEC_SELFTEST): fec_selftest.c fec.c fec.h
	$(CC) $(CFLAGS) -o $@ fec_selftest.c fec.c

test-fec: $(FEC_SELFTEST)
	./$(FEC_SELFTEST)

$(RTP_REORDER_SELFTEST): rtp_reorder_selftest.c rtp_reorder.h
	$(CC) $(CFLAGS) -o $@ rtp_reorder_selftest.c

test-rtp-reorder: $(RTP_REORDER_SELFTEST)
	./$(RTP_REORDER_SELFTEST)

$(ENCODE_CADENCE_SELFTEST): encode_cadence_selftest.c encode_cadence.h
	$(CC) $(CFLAGS) -o $@ encode_cadence_selftest.c

test-encode-cadence: $(ENCODE_CADENCE_SELFTEST)
	./$(ENCODE_CADENCE_SELFTEST)

$(RTP_LOSS_RECOVERY_SELFTEST): rtp_loss_recovery_selftest.c rtp_loss_recovery.h
	$(CC) $(CFLAGS) -o $@ rtp_loss_recovery_selftest.c

test-rtp-loss-recovery: $(RTP_LOSS_RECOVERY_SELFTEST)
	./$(RTP_LOSS_RECOVERY_SELFTEST)

$(RECEIVER_CONFIG_SELFTEST): receiver_config_selftest.c receiver_config.h
	$(CC) $(CFLAGS) -o $@ receiver_config_selftest.c

test-receiver-config: $(RECEIVER_CONFIG_SELFTEST)
	./$(RECEIVER_CONFIG_SELFTEST)

$(SCALE_COVER_SELFTEST): scale_cover_selftest.c scale_cover.h
	$(CC) $(CFLAGS) -o $@ scale_cover_selftest.c

test-scale-cover: $(SCALE_COVER_SELFTEST)
	./$(SCALE_COVER_SELFTEST)

test: test-fec test-rtp-reorder test-encode-cadence test-rtp-loss-recovery test-receiver-config test-scale-cover

clean:
	rm -f $(SENDER) $(RECEIVER) $(RELAY) $(FEC_SELFTEST) $(RTP_REORDER_SELFTEST) $(ENCODE_CADENCE_SELFTEST) $(RTP_LOSS_RECOVERY_SELFTEST) $(RECEIVER_CONFIG_SELFTEST) $(SCALE_COVER_SELFTEST) $(RTP_LOCAL_LIB) libcamera_capture.o libcamera_capture_stub.o d405_capture.o d405_capture_stub.o

.PHONY: all clean test test-fec test-rtp-reorder test-encode-cadence test-rtp-loss-recovery test-receiver-config test-scale-cover
