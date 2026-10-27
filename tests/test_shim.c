/* CPU-only tests of the production interposer; no NVIDIA device is opened. */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static atomic_int resolutions, version_reads, target_reads, identity_probes, fallback_calls, log_messages;
static int fail_resolution, nvidia_fd = -1;
static char directory[] = "/tmp/gpu-power-shim.XXXXXX", version_path[256], target_path[256];
static void *test_dlsym(void *, const char *);
static char *test_dlerror(void);
static int test_openat(int, const char *, int, ...);
static int test_fstat(int, struct stat *);
static long test_syscall(long, ...);
static ssize_t test_write(int, const void *, size_t);
static int backend_ioctl(int, unsigned long, ...);
#define dlsym test_dlsym
#define dlerror test_dlerror
#define openat test_openat
#define fstat test_fstat
#define syscall test_syscall
#define write test_write
#define ioctl shim_ioctl
#define close shim_close
#ifndef SHIM_SOURCE
#define SHIM_SOURCE "../src/powerd-shim.c"
#endif
#include SHIM_SOURCE
#undef dlsym
#undef dlerror
#undef openat
#undef fstat
#undef syscall
#undef write
#undef ioctl
#undef close

static void *test_dlsym(void *handle, const char *name)
{
    (void)handle;
    if (!strcmp(name, "ioctl")) {
        atomic_fetch_add(&resolutions, 1);
        usleep(10000); /* Widen the old unsynchronised initialization race. */
        return fail_resolution ? NULL : (void *)backend_ioctl;
    }
    assert(!strcmp(name, "close"));
    return (void *)close;
}
static char *test_dlerror(void) { return fail_resolution ? "forced lookup failure" : NULL; }
static int test_openat(int dir, const char *path, int flags, ...)
{
    if (!strcmp(path, "/sys/module/nvidia/version")) {
        atomic_fetch_add(&version_reads, 1); path = version_path;
    } else if (!strcmp(path, "/run/gpu-power-policy/target-mw")) {
        atomic_fetch_add(&target_reads, 1); path = target_path;
    }
    return openat(dir, path, flags);
}
static int test_fstat(int fd, struct stat *st)
{
    if (fd != nvidia_fd) return fstat(fd, st);
    memset(st, 0, sizeof(*st));
    st->st_mode = S_IFCHR | 0600; st->st_dev = 1; st->st_ino = 42; st->st_rdev = makedev(195, 255);
    return 0;
}
static long test_syscall(long command, ...)
{
    va_list args; va_start(args, command);
    int fd = va_arg(args, int);
    long result;
    if (command == SYS_close) result = syscall(SYS_close, fd);
    else {
        assert(command == SYS_ioctl);
        unsigned long request = va_arg(args, unsigned long);
        void *arg = va_arg(args, void *);
        atomic_fetch_add(&fallback_calls, 1);
        result = syscall(SYS_ioctl, fd, request, arg);
    }
    va_end(args); return result;
}
static ssize_t test_write(int fd, const void *data, size_t size)
{
    if (size >= 15 && !memcmp(data, "gpu-power shim:", 15)) atomic_fetch_add(&log_messages, 1);
    return write(fd, data, size);
}
static int backend_ioctl(int fd, unsigned long request, ...)
{
    va_list args; va_start(args, request); void *arg = va_arg(args, void *); va_end(args);
    if (fd != nvidia_fd) { errno = ENOTTY; return -1; }
    if (request == RM_CONTROL) {
        struct control *ctl = arg;
        if (ctl->command == 0x20800142) {
            atomic_fetch_add(&identity_probes, 1); *(uint32_t *)(uintptr_t)ctl->params = 42;
        }
    } else if ((request & 255) == 200) {
        atomic_fetch_add(&identity_probes, 1);
        uint8_t *cards = arg; cards[0] = 1; cards[8] = 10;
        uint32_t id = 42; memcpy(cards + 16, &id, 4);
    }
    return 0;
}
static void write_text(const char *path, const char *text)
{
    FILE *file = fopen(path, "w"); assert(file);
    assert(fputs(text, file) >= 0); assert(fclose(file) == 0);
}
static void reset(void)
{
    /* Only used after every worker has joined. */
    initialized = PTHREAD_ONCE_INIT;
    real_ioctl = NULL; real_close = NULL; version_matches = 0; configured_bdf[0] = 0;
    memset(matches, 0, sizeof(matches));
    resolutions = 0; version_reads = 0; target_reads = 0;
    identity_probes = 0; fallback_calls = 0; log_messages = 0;
}
static void *fallback_thread(void *argument)
{
    int fd = *(int *)argument, value = 0;
    assert(shim_ioctl(fd, FIONREAD, &value) == -1); assert(errno == ENOTTY);
    return NULL;
}
static void *matching_thread(void *argument)
{
    int fd = *(int *)argument;
    uint8_t raw[0x328] = {0}; raw[4] = 4; raw[0x30] = 0xf7;
    struct control ctl = {1, 2, 0x2080e634, 0, (uintptr_t)raw, sizeof(raw), 0};
    assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(word(raw + 0x2c) == 125000);
    return NULL;
}
static void workers(void *(*function)(void *), int *fd)
{
    pthread_t threads[8];
    for (size_t i = 0; i < 8; i++) assert(!pthread_create(&threads[i], NULL, function, fd));
    for (size_t i = 0; i < 8; i++) assert(!pthread_join(threads[i], NULL));
}
int main(void)
{
    assert(mkdtemp(directory));
    snprintf(version_path, sizeof(version_path), "%s/version", directory);
    snprintf(target_path, sizeof(target_path), "%s/target", directory);
    write_text(version_path, "615.71.09\n"); write_text(target_path, "125000\n");
    setenv("GPU_POWER_DEVICE", "0000:0A:00.0", 1); setenv("GPU_POWER_CEILING_MW", "140000", 1);
    int fd = open("/dev/null", O_RDWR); assert(fd >= 0);
    fail_resolution = 1; workers(fallback_thread, &fd);
    assert(resolutions == 1 && fallback_calls == 8 && log_messages == 1);
    puts("shim: NULL resolver falls back to SYS_ioctl, logs once, resolves once across eight threads");
    fail_resolution = 0; reset();
    assert(shim_ioctl(fd, RM_CONTROL, (void *)1) == -1); /* Must not dereference this non-NVIDIA argument. */
    assert(errno == ENOTTY && identity_probes == 0 && target_reads == 0);
    puts("shim: non-NVIDIA fd is forwarded without argument inspection or identity ioctls");
    nvidia_fd = fd; reset(); workers(matching_thread, &fd);
    assert(resolutions == 1 && version_reads == 1 && identity_probes == 2 && target_reads == 8);
    puts("shim: uppercase BDF accepted; concurrent requests share version and per-fd GPU checks");
    uint8_t raw[0x328] = {0}, expected[0x328]; raw[4] = 4; raw[0x30] = 0xf7;
    struct control ctl = {1, 2, 0x2080e634, 0, (uintptr_t)raw, sizeof(raw), 0};
    memcpy(expected, raw, sizeof(raw));
    unlink(target_path); assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0);
    assert(!memcmp(raw, expected, sizeof(raw)));
    write_text(target_path, "140001\n"); assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0);
    assert(!memcmp(raw, expected, sizeof(raw)));
    write_text(target_path, "125000\n"); ctl.size = 0x188;
    assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(!memcmp(raw, expected, sizeof(raw)));
    ctl.size = sizeof(raw);
    raw[4] = 3; assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(word(raw + 0x2c) == 0);
    raw[4] = 0; assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(word(raw + 0x2c) == 0);
    raw[4] = 4; raw[0x30] = 0; assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(word(raw + 0x2c) == 0);
    raw[0x30] = 0xf7; assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0);
    uint32_t value = 125000; memcpy(expected + 0x2c, &value, 4);
    assert(!memcmp(raw, expected, sizeof(raw)));
    assert(shim_ioctl(fd, 0xc0104629UL, raw) == 0); assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0);
    assert(identity_probes == 4);
    ctl.object++; assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(identity_probes == 6);
    assert(shim_close(fd) == 0);
    int replacement = open("/dev/null", O_RDWR); assert(replacement == fd);
    assert(shim_ioctl(replacement, RM_CONTROL, &ctl) == 0); assert(identity_probes == 8);
    puts("shim: cache invalidates on RM_FREE, changed object and close/fd reuse");
    reset(); write_text(version_path, "595.99.02\n"); memset(raw + 0x2c, 0, 4);
    assert(shim_ioctl(fd, RM_CONTROL, &ctl) == 0); assert(word(raw + 0x2c) == 0 && identity_probes == 0);
    assert(shim_close(fd) == 0); unlink(version_path); unlink(target_path); assert(!rmdir(directory));
    puts("shim: packet, version, target removal and ceiling gates passed; temporary fixtures removed");
    return 0;
}
