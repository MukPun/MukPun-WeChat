#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Observation only: no alternate resolver, retries, or address inspection. */
static pthread_once_t resolver_once = PTHREAD_ONCE_INIT;
static int (*real_getaddrinfo)(const char *, const char *,
                              const struct addrinfo *, struct addrinfo **);

static void resolve_next(void)
{
    real_getaddrinfo = (int (*)(const char *, const char *,
                               const struct addrinfo *, struct addrinfo **))
        dlsym(RTLD_NEXT, "getaddrinfo");
}

static int diagnostic_fd(void)
{
    const char *value = getenv("AW_LOGIN_DIAG_FD");
    int fd = 0;
    size_t digits = 0;
    if (value == NULL)
        return -1;
    for (; *value != '\0'; ++value) {
        if (++digits > 4 || *value < '0' || *value > '9')
            return -1;
        fd = fd * 10 + (*value - '0');
    }
    return digits != 0 && fd >= 3 && fd <= 1023 ? fd : -1;
}

static int canonical_host(const char *node, char out[254])
{
    size_t length = 0, label = 0;
    if (node == NULL)
        return 0;
    for (; length < 254 && node[length] != '\0'; ++length) {
        unsigned char c = (unsigned char)node[length];
        if (length == 253)
            return 0;
        if (c >= 'A' && c <= 'Z')
            c = (unsigned char)(c + ('a' - 'A'));
        if (c == '.') {
            if (label == 0 || out[length - 1] == '-')
                return 0;
            label = 0;
        } else {
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')
                || (label == 0 && c == '-') || ++label > 63)
                return 0;
        }
        out[length] = (char)c;
    }
    if (length == 0 || label == 0 || out[length - 1] == '-')
        return 0;
    out[length] = '\0';
    return (length > 11 && strcmp(out + length - 11, ".wechat.com") == 0)
        || (length > 14 && strcmp(out + length - 14, ".weixin.qq.com") == 0);
}

static void write_diagnostic(const char *node, int result)
{
    char host[254], line[512];
    struct stat info;
    sigset_t blocked, previous, pending;
    const struct timespec zero = {0, 0};
    int fd, flags, length, had_sigpipe;

    if (!canonical_host(node, host) || (fd = diagnostic_fd()) < 0)
        return;
    flags = fcntl(fd, F_GETFL);
    if (flags < 0 || !(flags & O_NONBLOCK) || fstat(fd, &info) != 0
        || !S_ISFIFO(info.st_mode))
        return;
    length = snprintf(line, sizeof(line), "{\"host\":\"%s\",\"result\":%d}\n", host, result);
    if (length <= 0 || (size_t)length >= sizeof(line))
        return;

    /* A closed reader must not deliver SIGPIPE to the native caller. */
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGPIPE);
    if (pthread_sigmask(SIG_BLOCK, &blocked, &previous) != 0)
        return;
    if (sigpending(&pending) != 0) {
        (void)pthread_sigmask(SIG_SETMASK, &previous, NULL);
        return;
    }
    had_sigpipe = sigismember(&pending, SIGPIPE);
    if (write(fd, line, (size_t)length) < 0 && errno == EPIPE && had_sigpipe == 0) {
        /* Consume only a newly generated SIGPIPE, without waiting. */
        while (sigtimedwait(&blocked, NULL, &zero) < 0 && errno == EINTR) {}
    }
    (void)pthread_sigmask(SIG_SETMASK, &previous, NULL);
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **addresses)
{
    int original_errno = errno;
    int result, resolver_errno;
    (void)pthread_once(&resolver_once, resolve_next);
    if (real_getaddrinfo == NULL) {
        errno = ENOSYS;
        return EAI_SYSTEM;
    }
    errno = original_errno;
    result = real_getaddrinfo(node, service, hints, addresses);
    resolver_errno = errno;
    write_diagnostic(node, result);
    errno = resolver_errno;
    return result;
}
