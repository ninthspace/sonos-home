#include <spawn.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    // child inherits this bundle's TCC responsibility; argv[1..] = command
    pid_t pid; int st = 0;
    if (argc < 2) return 2;
    if (posix_spawn(&pid, argv[1], NULL, NULL, argv + 1, environ) != 0) return 3;
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
