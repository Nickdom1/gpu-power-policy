#!/usr/bin/env python3
"""Read-only, exact-library/PCI power-constraint gate before a powerd change."""

import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path

LIBRARY = Path("@nvml@")
EXPECTED = "5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181"


def check(lib, max_mw=125000):
    def call(name, *args):
        rc = getattr(lib, name)(*args)
        if rc:
            raise RuntimeError(f"{name}: NVML {rc}")

    call("nvmlInit_v2")
    try:
        handle = C.c_void_p()
        call(
            "nvmlDeviceGetHandleByPciBusId_v2",
            C.c_char_p(b"0000:01:00.0"),
            C.byref(handle),
        )
        lo, hi, rated = C.c_uint(), C.c_uint(), C.c_uint()
        call(
            "nvmlDeviceGetPowerManagementLimitConstraints",
            handle,
            C.byref(lo),
            C.byref(hi),
        )
        call("nvmlDeviceGetPowerManagementDefaultLimit", handle, C.byref(rated))
        values = (lo.value, rated.value, hi.value)
        print(json.dumps({"pci": "0000:01:00.0", "min_rated_max_mW": values}))
        if values != (1000, 80000, max_mw):
            raise RuntimeError(f"expected policy constraints 1000/80000/{max_mw} mW")
    finally:
        call("nvmlShutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-mw",
        type=int,
        default=125000,
        choices=(125000, 140000),
        help="policy 2 maximum the booted image is expected to carry",
    )
    args = parser.parse_args()
    try:
        if hashlib.sha256(LIBRARY.read_bytes()).hexdigest() != EXPECTED:
            raise RuntimeError("NVML library hash differs")
        check(C.CDLL(str(LIBRARY)), args.max_mw)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"STOP: {exc}; no powerd change follows")
