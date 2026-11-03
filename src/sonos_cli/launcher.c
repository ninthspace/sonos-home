// The launchd-facing half of the Sonos service: a tiny app-bundle executable that runs its arguments as a child
// process and waits. macOS attributes Local Network access to this signed bundle rather than to the Python binary,
// so the permission survives a Python upgrade. Signals are passed on so launchd can stop the service cleanly; one
// that arrives before the child exists is remembered and passed on as soon as it does.
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;
static volatile sig_atomic_t early_signal = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
    else early_signal = sig;
}

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    pid_t pid;
    if (posix_spawn(&pid, argv[1], NULL, NULL, argv + 1, environ) != 0) return 3;
    const char *delay = getenv("SONOS_LAUNCHER_SPAWN_DELAY_US");       // tests widen the window between spawn and `child = pid`; launchd never sets this
    if (delay) {
        long us = strtol(delay, NULL, 10);
        if (us > 0 && us <= 2000000) usleep((useconds_t)us);            // anything else (negative, huge, not a number) is ignored
    }
    child = pid;
    if (early_signal) kill(pid, early_signal);          // the signal came in while the child was being started
    int st = 0;
    while (waitpid(pid, &st, 0) < 0) {
        if (errno != EINTR) return 4;
    }
    if (WIFEXITED(st)) return WEXITSTATUS(st);
    return 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
}
