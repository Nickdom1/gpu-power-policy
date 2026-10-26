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
#ifndef SHIM_TEST
#include <dlfcn.h>
#include <stdarg.h>
#endif

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
    FILE *file = fopen(path, "r");
    if (!file) return 0;
    size_t count = fread(text, 1, size - 1, file);
    int ok = !ferror(file) && feof(file);
    text[count] = 0;
    fclose(file);
    return ok;
}

static void rewrite(unsigned long request, struct control *ctl, const char *version,
                    const char *target, uint32_t ceiling)
{
    if (request != RM_CONTROL || !ctl || ctl->command != 0x2080e634 ||
        ctl->size != 0x328 || !ctl->params) return;
    char text[64];
    if (!read_text(version, text, sizeof(text)) || strcmp(text, "615.71.09\n")) return;
    uint8_t *params = (uint8_t *)(uintptr_t)ctl->params;
    uint32_t mask = word(params + 4);
    if (!mask || (mask & (mask - 1)) || params[0x30] != 0xf7) return;
    if (!read_text(target, text, sizeof(text))) return;
    uint32_t value = number(text);
    if (value && value <= ceiling) memcpy(params + 0x2c, &value, sizeof(value));
}

#ifndef SHIM_TEST
static int (*real_ioctl)(int, unsigned long, ...);

static int selected_gpu(int fd, const struct control *ctl)
{
    const char *bdf = getenv("GPU_POWER_DEVICE");
    if (!bdf) return 0;
    uint32_t id = 0;
    struct control query = {ctl->client, ctl->object, 0x20800142, 0, (uintptr_t)&id, 4, 0};
    if (real_ioctl(fd, RM_CONTROL, &query) || query.status) return 0;
    uint8_t cards[32 * 72] = {0};
    if (real_ioctl(fd, 0xc00046c8UL | (sizeof(cards) << 16), cards)) return 0;
    for (size_t off = 0; off < sizeof(cards); off += 72) {
        char found[32];
        if (!cards[off] || word(cards + off + 16) != id) continue;
        snprintf(found, sizeof(found), "%04x:%02x:%02x.%u", word(cards + off + 4),
                 cards[off + 8], cards[off + 9], cards[off + 10]);
        if (!strcmp(found, bdf)) return 1;
    }
    return 0;
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list args;
    va_start(args, request);
    void *arg = va_arg(args, void *);
    va_end(args);
    if (!real_ioctl) real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    if (!real_ioctl) { errno = ENOSYS; return -1; }
    struct control *ctl = arg;
    int saved_errno = errno;
    char version[32];
    if (request == RM_CONTROL && ctl && ctl->command == 0x2080e634 && ctl->size == 0x328 &&
        read_text("/sys/module/nvidia/version", version, sizeof(version)) &&
        !strcmp(version, "615.71.09\n") && selected_gpu(fd, ctl))
        rewrite(request, ctl, "/sys/module/nvidia/version", "/run/gpu-power-policy/target-mw",
                number(getenv("GPU_POWER_CEILING_MW")));
    errno = saved_errno;
    return real_ioctl(fd, request, arg);
}
#else
static void write_text(const char *path, const char *text)
{
    FILE *file = fopen(path, "w");
    assert(file);
    assert(fputs(text, file) >= 0);
    assert(fclose(file) == 0);
}

int main(void)
{
    uint8_t raw[0x328] = {0}, expected[0x328];
    struct control ctl = {1, 2, 0x2080e634, 0, (uintptr_t)raw, sizeof(raw), 0};
    raw[4] = 4;
    raw[0x30] = 0xf7;
    memcpy(expected, raw, sizeof(raw));
    write_text("version", "615.71.09\n");
    remove("target");
    rewrite(RM_CONTROL, &ctl, "version", "target", 140000);
    assert(!memcmp(raw, expected, sizeof(raw)));
    write_text("target", "125000\n");
    write_text("version", "595.99.02\n");
    rewrite(RM_CONTROL, &ctl, "version", "target", 140000);
    assert(!memcmp(raw, expected, sizeof(raw)));
    write_text("version", "615.71.09\n");
    write_text("target", "140001\n");
    rewrite(RM_CONTROL, &ctl, "version", "target", 140000);
    assert(!memcmp(raw, expected, sizeof(raw)));
    write_text("target", "125000\n");
    ctl.size = 0x188;
    rewrite(RM_CONTROL, &ctl, "version", "target", 140000);
    assert(!memcmp(raw, expected, sizeof(raw)));
    ctl.size = sizeof(raw);
    rewrite(RM_CONTROL, &ctl, "version", "target", 140000);
    uint32_t target = 125000;
    memcpy(expected + 0x2c, &target, 4);
    assert(!memcmp(raw, expected, sizeof(raw)));
    remove("version");
    remove("target");
    puts("shim: round-trip, absent file, version, size and ceiling checks passed");
    return 0;
}
#endif
