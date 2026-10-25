#!/usr/bin/env python3
"""Fixed one-request driver for the GDB shim; selects the verified PCI address."""

import ctypes as C
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "nvml_limit", Path(__file__).resolve().parent.parent / "nvml-limit.py"
)
nvml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nvml)
rc, msg = nvml.call("nvmlInit_v2")
if rc:
    raise SystemExit("nvmlInit: " + msg)
try:
    handle = C.c_void_p()
    rc, msg = nvml.call(
        "nvmlDeviceGetHandleByPciBusId_v2", C.c_char_p(b"0000:01:00.0"), C.byref(handle)
    )
    if rc:
        raise SystemExit("device: " + msg)
    print("before:", nvml.limits(handle), flush=True)
    rc, msg = nvml.call("nvmlDeviceSetPowerManagementLimit", handle, C.c_uint(125000))
    print("set 125.0 W:", msg, flush=True)
    print("after: ", nvml.limits(handle), flush=True)
finally:
    nvml.lib.nvmlShutdown()
