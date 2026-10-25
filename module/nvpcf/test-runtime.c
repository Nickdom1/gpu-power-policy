/* CPU-only: actual pinned filter source; no ioctl wrapper or device access. */
#define NVPCF_SHIM_NO_WRAPPER
#include "nvpcf-shim.c"
#include <assert.h>

static unsigned long req(unsigned nr, unsigned size)
{
    return ((unsigned long)(_IOC_READ | _IOC_WRITE) << _IOC_DIRSHIFT) |
           ((unsigned long)NV_IOCTL_MAGIC << _IOC_TYPESHIFT) |
           ((unsigned long)nr << _IOC_NRSHIFT) |
           ((unsigned long)size << _IOC_SIZESHIFT);
}

int main(void)
{
    struct { nvos54_t p; unsigned char b[0x188]; } f = {0};
    unsigned char expected[0x188];
    uint32_t *limit = (uint32_t *)(f.b + 12);
    unsigned long ctl = req(NV_ESC_RM_CONTROL, sizeof(nvos54_t));
    /* PMGR tracing: off by default, logs once per change when on. */
    {
        struct { nvos54_t p; unsigned char b[0x188]; } c;
        memset(&c, 0, sizeof(c));
        c.p.cmd = 0x2080E634u; c.p.params = (uintptr_t)c.b; c.p.paramsSize = sizeof(c.b);
        unsetenv("NVPCF_SHIM_TRACE_PMGR"); nvpcf_shim_configure();
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 0);
        setenv("NVPCF_SHIM_TRACE_PMGR", "1", 1); nvpcf_shim_configure();
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 1);
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 0);              /* unchanged: silent */
        c.b[8] = 0x42;
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 1);              /* changed bytes: logged */
        c.p.status = 0x1f;
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 1);              /* changed status: logged */
        c.p.cmd = 0x2080A618u;
        assert(nvpcf_shim_trace(ctl, &c.p, 0) == 0);              /* not a traced command */
        unsetenv("NVPCF_SHIM_TRACE_PMGR"); nvpcf_shim_configure();
    }
    f.p.cmd = 0x2080e634;
    f.p.params = (uintptr_t)f.b;
    f.p.paramsSize = sizeof(f.b);
    memset(f.b, 0x5a, sizeof(f.b));
    *limit = 115000;
    memcpy(expected, f.b, sizeof(f.b));
    unsetenv("NVPCF_SHIM_KERNEL_LIMIT_MW");
    nvpcf_shim_configure();
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    assert(memcmp(f.b, expected, sizeof(f.b)) == 0);
    unsetenv("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW");
    setenv("NVPCF_SHIM_KERNEL_LIMIT_MW", "125000", 1);
    nvpcf_shim_configure();
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    assert(memcmp(f.b, expected, sizeof(f.b)) == 0);
    setenv("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW", "125000", 1);
    nvpcf_shim_configure();
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 1);
    uint32_t target = 125000;
    memcpy(expected + 12, &target, 4);
    assert(memcmp(f.b, expected, sizeof(f.b)) == 0);
    setenv("NVPCF_SHIM_KERNEL_LIMIT_MW", "126000", 1);
    nvpcf_shim_configure();
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    assert(memcmp(f.b, expected, sizeof(f.b)) == 0);
    setenv("NVPCF_SHIM_KERNEL_LIMIT_MW", "125000", 1);
    nvpcf_shim_configure();
    f.p.cmd = 0x2080e61e;
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    f.p.cmd = 0x2080e634;
    f.p.paramsSize = 0x180;
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    f.p.paramsSize = 0x188;
    *limit = 0;
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0 && *limit == 0);
    *limit = 115000;
    assert(nvpcf_shim_rewrite_request(ctl, NULL) == 0);
    assert(nvpcf_shim_rewrite_request(req(0x10, 8), &f.p) == 0);
    assert(nvpcf_shim_rewrite_request(ctl ^ ((unsigned long)(NV_IOCTL_MAGIC ^ 0x47) << _IOC_TYPESHIFT), &f.p) == 0);
    assert(nvpcf_shim_rewrite_request(req(NV_ESC_RM_CONTROL, 31), &f.p) == 0);
    f.p.params = 0;
    assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0);
    f.p.params = (uintptr_t)f.b;
    nv_xfer_t x = { .cmd = NV_ESC_RM_CONTROL, .size = sizeof(nvos54_t), .ptr = (uintptr_t)&f.p };
    unsigned long xfer = req(NV_ESC_IOCTL_XFER_CMD, sizeof(x));
    assert(nvpcf_shim_rewrite_request(xfer, &x) == 1 && *limit == 125000);
    *limit = 115000;
    x.size = 31;
    assert(nvpcf_shim_rewrite_request(xfer, &x) == 0 && *limit == 115000);
    x.size = sizeof(nvos54_t); x.cmd = 0;
    assert(nvpcf_shim_rewrite_request(xfer, &x) == 0);
    x.cmd = NV_ESC_RM_CONTROL; x.ptr = 0;
    assert(nvpcf_shim_rewrite_request(xfer, &x) == 0);
    const char *invalid[] = { "", "abc", "-5", "999999", "125000x", "99999999999999999999999999" };
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        setenv("NVPCF_SHIM_KERNEL_LIMIT_MW", invalid[i], 1);
        nvpcf_shim_configure();
        assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0 && *limit == 115000);
        setenv("NVPCF_SHIM_KERNEL_LIMIT_MW", "125000", 1);
        setenv("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW", invalid[i], 1);
        nvpcf_shim_configure();
        assert(nvpcf_shim_rewrite_request(ctl, &f.p) == 0 && *limit == 115000);
        setenv("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW", "125000", 1);
    }
    puts("125 W runtime cases PASS: explicit cap required, exact packet-only edit, 126 W refusal, command/layout/zero guards");
    return 0;
}
