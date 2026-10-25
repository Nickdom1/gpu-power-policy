/* Dynamic Boost target filter for nvidia-powerd 595.99.02.
 * Rewrites only the E634 target, bounded by NVPCF_SHIM_KERNEL_LIMIT_CAP_MW
 * (default 120000). The firmware decides whether to accept the request.
 * NVPCF_SHIM_KERNEL_LIMIT_MW selects the target; NVPCF_SHIM_TRACE_PMGR=1
 * logs power-policy requests and returned status. No ioctl is issued here.
 * Layouts were identified from daemon disassembly and NVIDIA's open module.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <syslog.h>

#define NV_IOCTL_MAGIC        'F'
#define NV_ESC_RM_CONTROL     0x2A
#define NV_ESC_IOCTL_XFER_CMD (200 + 11)

typedef struct {
    uint32_t hClient, hObject, cmd, flags;
    uint64_t params;
    uint32_t paramsSize, status;
} nvos54_t;

typedef struct {
    uint32_t cmd, size;
    uint64_t ptr;
} nv_xfer_t;

_Static_assert(sizeof(nvos54_t) == 32, "NVOS54_PARAMETERS is 32 bytes");
_Static_assert(offsetof(nvos54_t, params) == 16, "NVOS54 params at 16");
_Static_assert(sizeof(nv_xfer_t) == 16, "nv_ioctl_xfer_t is 16 bytes");

struct shim_config {
    int trace_pmgr;
    int have_kernel_limit;
    int32_t kernel_limit, kernel_cap;
};

static struct shim_config config;

static int parse_mw(const char *name, int32_t lo, int32_t hi, int32_t *out)
{
    const char *s = getenv(name);
    char *end;
    long v;

    if (s == NULL || *s == '\0')
        return 0;
    errno = 0;
    v = strtol(s, &end, 10);
    if (errno != 0 || *end != '\0' || v < lo || v > hi) {
        syslog(LOG_ERR, "nvpcf-shim: ignoring %s=%s (expected an integer %d..%d mW)", name, s, lo, hi);
        return 0;
    }
    *out = (int32_t)v;
    return 1;
}

/* Exposed for the unit test; the ioctl wrapper calls it once per process. */
void nvpcf_shim_configure(void)
{
    struct shim_config c = {0};
    {
        const char *tr = getenv("NVPCF_SHIM_TRACE_PMGR");
        c.trace_pmgr = tr != NULL && strcmp(tr, "1") == 0;
    }
    c.kernel_cap = 120000;
    parse_mw("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW", 0, 200000, &c.kernel_cap);
    c.have_kernel_limit = parse_mw("NVPCF_SHIM_KERNEL_LIMIT_MW", 1000, 200000, &c.kernel_limit);
    if (c.have_kernel_limit && c.kernel_limit > c.kernel_cap) {
        syslog(LOG_ERR, "nvpcf-shim: kernel limit %d mW above the %d mW cap; not replacing it",
               c.kernel_limit, c.kernel_cap);
        c.have_kernel_limit = 0;
    }
    config = c;
    if (c.have_kernel_limit)
        syslog(LOG_INFO, "nvpcf-shim: will send %d mW as the Dynamic Boost target", c.kernel_limit);
    syslog(LOG_INFO, "nvpcf-shim: loaded (trace_pmgr=%d)", c.trace_pmgr);
}

static nvos54_t *rm_control(unsigned long request, void *arg)
{
    if (arg == NULL || _IOC_TYPE(request) != NV_IOCTL_MAGIC)
        return NULL;
    if (_IOC_NR(request) == NV_ESC_RM_CONTROL && _IOC_SIZE(request) == sizeof(nvos54_t))
        return arg;
    if (_IOC_NR(request) == NV_ESC_IOCTL_XFER_CMD && _IOC_SIZE(request) == sizeof(nv_xfer_t)) {
        nv_xfer_t *x = arg;
        if (x->cmd == NV_ESC_RM_CONTROL && x->size == sizeof(nvos54_t) && x->ptr != 0)
            return (nvos54_t *)(uintptr_t)x->ptr;
    }
    return NULL;
}

static int traced_cmd(uint32_t cmd)
{
    return cmd == 0x2080E633u || cmd == 0x2080E634u || cmd == 0x2080E639u || cmd == 0x2080E0AFu;
}

/*
 * Log a privileged power-policy command after the driver returned, with the first
 * bytes of its parameters as hex, but only when command, status or bytes changed
 * since the last time that command was seen (the daemon repeats its target).
 * Exposed for the unit test. Returns 1 when a line was logged.
 */
int nvpcf_shim_trace(unsigned long request, void *arg, int rc)
{
    enum { SLOTS = 4, SHOWN = 64 };
    static uint32_t seen_cmd[SLOTS], seen_status[SLOTS];
    static unsigned char seen_bytes[SLOTS][SHOWN];
    static int used[SLOTS];
    nvos54_t *p;
    char hex[SHOWN * 2 + 1];
    unsigned n, i, slot;

    if (!config.trace_pmgr || (p = rm_control(request, arg)) == NULL || !traced_cmd(p->cmd) || p->params == 0)
        return 0;
    slot = (p->cmd == 0x2080E633u) ? 0 : (p->cmd == 0x2080E634u) ? 1 : (p->cmd == 0x2080E639u) ? 2 : 3;
    n = p->paramsSize < SHOWN ? p->paramsSize : SHOWN;
    if (used[slot] && seen_cmd[slot] == p->cmd && seen_status[slot] == p->status &&
        memcmp(seen_bytes[slot], (const void *)(uintptr_t)p->params, n) == 0)
        return 0;
    used[slot] = 1;
    seen_cmd[slot] = p->cmd;
    seen_status[slot] = p->status;
    memcpy(seen_bytes[slot], (const void *)(uintptr_t)p->params, n);
    for (i = 0; i < n; i++)
        snprintf(hex + 2 * i, 3, "%02x", ((const unsigned char *)(uintptr_t)p->params)[i]);
    hex[2 * n] = '\0';
    syslog(LOG_INFO, "nvpcf-shim: pmgr cmd 0x%08x size %u ioctl rc %d status 0x%x first %u bytes %s",
           p->cmd, p->paramsSize, rc, p->status, n, hex);
    return 1;
}


#define CMD_PMGR_SET_KERNEL_LIMIT 0x2080E634u
#define PMGR_SET_PARAMS_SIZE      0x188u
#define PMGR_SET_LIMIT_OFFSET     0x0Cu

/*
 * Before the ioctl: replace the limit nvidia-powerd sends for the KERNEL client
 * policy. Returns 1 when the value was replaced. Exposed for the unit test.
 */
int nvpcf_shim_rewrite_request(unsigned long request, void *arg)
{
    static uint32_t last_seen;
    nvos54_t *p;
    uint32_t *limit;

    if (!config.have_kernel_limit || (p = rm_control(request, arg)) == NULL ||
        p->cmd != CMD_PMGR_SET_KERNEL_LIMIT || p->params == 0 || p->paramsSize != PMGR_SET_PARAMS_SIZE)
        return 0;
    limit = (uint32_t *)((unsigned char *)(uintptr_t)p->params + PMGR_SET_LIMIT_OFFSET);
    if (*limit == 0)
        return 0;
    if (*limit != last_seen) {
        syslog(LOG_INFO, "nvpcf-shim: daemon target %u mW replaced by %d mW", *limit, config.kernel_limit);
        last_seen = *limit;
    }
    *limit = (uint32_t)config.kernel_limit;
    return 1;
}

#ifndef NVPCF_SHIM_NO_WRAPPER
static int (*real_ioctl)(int, unsigned long, ...);

__attribute__((constructor)) static void nvpcf_shim_init(void)
{
    real_ioctl = (int (*)(int, unsigned long, ...))dlsym(RTLD_NEXT, "ioctl");
    nvpcf_shim_configure();
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    void *arg;
    int rc;

    va_start(ap, request);
    arg = va_arg(ap, void *);
    va_end(ap);
    if (real_ioctl == NULL) {
        errno = ENOSYS;
        return -1;
    }
    nvpcf_shim_rewrite_request(request, arg);
    rc = real_ioctl(fd, request, arg);
    nvpcf_shim_trace(request, arg, rc);
    return rc;
}
#endif
