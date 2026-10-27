/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <dlfcn.h>
#include <stdarg.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <unistd.h>

struct control {
    uint32_t client, object, command, flags;
    uint64_t params;
    uint32_t size, status;
};
_Static_assert(sizeof(struct control) == 32, "NVOS54 size");
_Static_assert(offsetof(struct control, params) == 16, "NVOS54 pointer");
#define RM_CONTROL 0xc020462aUL

static uint32_t word(const void *p)
{
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static uint32_t number(const char *text)
{
    char *end;
    if (!text || *text < '0' || *text > '9') return 0;
    unsigned long value = strtoul(text, &end, 10);
    if (*end == '\n') end++;
    return *end || value > UINT32_MAX ? 0 : (uint32_t)value;
}

static int read_text(const char *path, char *text, size_t size)
{
    int fd = openat(AT_FDCWD, path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    size_t count = 0;
    int ok = 0;
    while (count < size - 1) {
        ssize_t got = read(fd, text + count, size - 1 - count);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) break;
        if (!got) { ok = 1; break; }
        count += (size_t)got;
    }
    text[count] = 0;
    /* Avoid entering our close interposer during pthread_once initialization. */
    syscall(SYS_close, fd);
    return ok;
}

static int (*real_ioctl)(int, unsigned long, ...);
static int (*real_close)(int);
static pthread_once_t initialized = PTHREAD_ONCE_INIT;
static int version_matches;
static char configured_bdf[32];
static uint32_t configured_ceiling;

static void log_text(const char *text)
{
    size_t left = strlen(text);
    while (left) {
        ssize_t sent = write(STDERR_FILENO, text, left);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) return;
        text += sent;
        left -= (size_t)sent;
    }
}

static void initialize(void)
{
    dlerror();
    real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    const char *error = dlerror();
    if (!real_ioctl) {
        const char prefix[] = "gpu-power shim: ioctl lookup failed; using SYS_ioctl: ";
        const char *message = error ? error : "no symbol";
        log_text(prefix);
        log_text(message);
        log_text("\n");
    }
    dlerror();
    real_close = dlsym(RTLD_NEXT, "close");
    (void)dlerror();
    char version[32], extra;
    version_matches = read_text("/sys/module/nvidia/version", version, sizeof(version)) &&
                      !strcmp(version, "615.71.09\n");
    const char *bdf = getenv("GPU_POWER_DEVICE");
    unsigned domain, bus, slot, function;
    if (bdf && strlen(bdf) == 12 &&
        sscanf(bdf, "%4x:%2x:%2x.%1x%c", &domain, &bus, &slot, &function, &extra) == 4 &&
        domain <= 0xffff && bus <= 0xff && slot <= 0x1f && function <= 7)
        snprintf(configured_bdf, sizeof(configured_bdf), "%04x:%02x:%02x.%u", domain, bus, slot, function);
    configured_ceiling = number(getenv("GPU_POWER_CEILING_MW"));
}

static int forward_ioctl(int fd, unsigned long request, void *arg)
{
    return real_ioctl ? real_ioctl(fd, request, arg) : (int)syscall(SYS_ioctl, fd, request, arg);
}

struct match {
    int valid, fd, selected;
    dev_t device, rdev;
    ino_t inode;
    uint32_t client, object;
};
static struct match matches[64];
static pthread_mutex_t matches_lock = PTHREAD_MUTEX_INITIALIZER;

static void forget_fd(int fd)
{
    pthread_mutex_lock(&matches_lock);
    struct match *entry = &matches[(unsigned)fd % 64];
    if (entry->valid && entry->fd == fd) entry->valid = 0;
    pthread_mutex_unlock(&matches_lock);
}

static int selected_gpu(int fd, const struct control *ctl, const struct stat *st)
{
    pthread_mutex_lock(&matches_lock);
    struct match *entry = &matches[(unsigned)fd % 64];
    if (entry->valid && entry->fd == fd && entry->device == st->st_dev &&
        entry->inode == st->st_ino && entry->rdev == st->st_rdev &&
        entry->client == ctl->client && entry->object == ctl->object) {
        int selected = entry->selected;
        pthread_mutex_unlock(&matches_lock);
        return selected;
    }
    uint32_t id = 0;
    struct control query = {ctl->client, ctl->object, 0x20800142, 0, (uintptr_t)&id, 4, 0};
    uint8_t cards[32 * 72] = {0};
    int selected = 0;
    if (forward_ioctl(fd, RM_CONTROL, &query) || query.status ||
        forward_ioctl(fd, 0xc00046c8UL | (sizeof(cards) << 16), cards)) goto done;
    for (size_t off = 0; off < sizeof(cards); off += 72) {
        char found[32];
        if (!cards[off] || word(cards + off + 16) != id) continue;
        snprintf(found, sizeof(found), "%04x:%02x:%02x.%u", word(cards + off + 4),
                 cards[off + 8], cards[off + 9], cards[off + 10]);
        if (!strcmp(found, configured_bdf)) { selected = 1; break; }
    }
    *entry = (struct match){1, fd, selected, st->st_dev, st->st_rdev, st->st_ino, ctl->client, ctl->object};
 done:
    pthread_mutex_unlock(&matches_lock);
    return selected;
}

static void rewrite(struct control *ctl)
{
    if (!ctl->params) return;
    uint8_t *params = (uint8_t *)(uintptr_t)ctl->params;
    uint32_t mask = word(params + 4);
    if (!mask || (mask & (mask - 1)) || params[0x30] != 0xf7) return;
    char text[64];
    if (!read_text("/run/gpu-power-policy/target-mw", text, sizeof(text))) return;
    uint32_t value = number(text);
    if (value && value <= configured_ceiling) memcpy(params + 0x2c, &value, sizeof(value));
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list args;
    va_start(args, request);
    void *arg = va_arg(args, void *);
    va_end(args);
    int saved_errno = errno;
    pthread_once(&initialized, initialize);
    if (request == 0xc0104629UL) forget_fd(fd); /* RM_FREE can recycle client/object handles. */
    struct stat st;
    if (request == RM_CONTROL && arg && version_matches && configured_bdf[0] && configured_ceiling &&
        fstat(fd, &st) == 0 && S_ISCHR(st.st_mode) && major(st.st_rdev) == 195) {
        struct control *ctl = arg;
        if (ctl->command == 0x2080e634 && ctl->size == 0x328 && selected_gpu(fd, ctl, &st))
            rewrite(ctl);
    }
    errno = saved_errno;
    return forward_ioctl(fd, request, arg);
}

int close(int fd)
{
    int saved_errno = errno;
    pthread_once(&initialized, initialize);
    forget_fd(fd);
    errno = saved_errno;
    return real_close ? real_close(fd) : (int)syscall(SYS_close, fd);
}
