#ifndef PLATFORM_NET_H
#define PLATFORM_NET_H

#if defined(_WIN32) || defined(__MINGW32__)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <errno.h>
#include <fcntl.h>
#include <io.h>

#ifndef ssize_t
#ifdef _WIN64
typedef __int64 ssize_t;
#else
typedef int ssize_t;
#endif
#endif

#ifndef MSG_DONTWAIT
#define MSG_DONTWAIT 0
#endif

static inline int platform_net_init(void) {
    WSADATA wsa;
    return WSAStartup(MAKEWORD(2, 2), &wsa);
}

static inline void platform_net_shutdown(void) {
    WSACleanup();
}

static inline int socket_set_nonblock(int fd) {
    u_long mode = 1;
    return ioctlsocket((SOCKET)fd, FIONBIO, &mode);
}

static inline int socket_close(int fd) {
    return closesocket((SOCKET)fd);
}

static inline int socket_would_block(void) {
    int err = WSAGetLastError();
    // UDP may report ICMP port-unreachable from an earlier datagram as
    // WSAECONNRESET. It is not a local receiver failure; keep receiving.
    return err == WSAEWOULDBLOCK || err == WSAEINTR || err == WSAECONNRESET;
}

static inline int socket_last_error(void) {
    return WSAGetLastError();
}

#else
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

static inline int platform_net_init(void) {
    return 0;
}

static inline void platform_net_shutdown(void) {}

static inline int socket_set_nonblock(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return -1;
    }
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static inline int socket_close(int fd) {
    return close(fd);
}

static inline int socket_would_block(void) {
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
}

static inline int socket_last_error(void) {
    return errno;
}

#endif

#endif /* PLATFORM_NET_H */
