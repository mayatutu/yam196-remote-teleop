#ifndef YAM_DEPLOYMENT_GUARD_H
#define YAM_DEPLOYMENT_GUARD_H
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <errno.h>
static int yam_deployment_video_allowed(void) {
    pid_t child = fork();
    if (child < 0) return 0;
    if (child == 0) {
        execl("/home/ai/yam-teleop/current/.venv/bin/python", "python",
              "/home/ai/yam-teleop/current/launch.py", "guard", "video", (char *)0);
        _exit(127);
    }
    int status = 0;
    pid_t result;
    do { result = waitpid(child, &status, 0); } while (result < 0 && errno == EINTR);
    return result == child && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}
#endif
