#!/usr/bin/env python3
"""NVML calls and limit readback used by the guarded role-8 request driver."""

import ctypes

lib = ctypes.CDLL("@nvml@")
ERR = {
    0: "SUCCESS",
    1: "UNINITIALIZED",
    2: "INVALID_ARGUMENT",
    3: "NOT_SUPPORTED",
    4: "NO_PERMISSION",
}


def call(name, *args):
    rc = getattr(lib, name)(*args)
    return rc, ERR.get(rc, f"error {rc}")


def mw(name, handle):
    v = ctypes.c_uint(0)
    rc, msg = call(name, handle, ctypes.byref(v))
    return f"{v.value / 1000:.1f} W" if rc == 0 else msg


def limits(handle):
    lo, hi = ctypes.c_uint(0), ctypes.c_uint(0)
    rc, msg = call(
        "nvmlDeviceGetPowerManagementLimitConstraints",
        handle,
        ctypes.byref(lo),
        ctypes.byref(hi),
    )
    span = f"{lo.value / 1000:.1f}..{hi.value / 1000:.1f} W" if rc == 0 else msg
    return (
        f"default {mw('nvmlDeviceGetPowerManagementDefaultLimit', handle)}, constraints {span}, "
        f"requested {mw('nvmlDeviceGetPowerManagementLimit', handle)}, "
        f"enforced {mw('nvmlDeviceGetEnforcedPowerLimit', handle)}, draw {mw('nvmlDeviceGetPowerUsage', handle)}"
    )
