#!/usr/bin/env python3
"""Root-run GET bootstrap. CPU tests use a separate synthetic program."""

import ctypes as C
import hashlib
import json
import os
from pathlib import Path
from codec import EXPECTED, CANARY, request


def main():
    out = Path(os.environ["PT516_POLICY_DIR"])
    query = os.environ["PT516_POLICY_QUERY"]
    pci = Path("/sys/bus/pci/devices/0000:01:00.0")
    if (pci / "vendor").read_text().strip() != "0x10de" or (
        pci / "device"
    ).read_text().strip() != "0x2460":
        raise SystemExit("POLICY STOP: PCI identity differs")
    library = Path("/run/opengl-driver/lib/libnvidia-ml.so.1")
    with library.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != EXPECTED:
            raise SystemExit("POLICY STOP: NVML hash differs before loading")
    info = (out.parent / "info/a618.bin").read_bytes() if query == "status" else None
    _, data = request(query, info)
    storage = C.create_string_buffer(CANARY + data + CANARY)
    (out / "buffer.json").write_text(
        json.dumps(
            dict(
                pid=os.getpid(),
                pointer=C.addressof(storage) + len(CANARY),
                size=len(data),
            )
        )
    )
    lib = C.CDLL(str(library))

    def call(name, types, *args):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = types, C.c_int
        rc = fn(*args)
        if rc:
            raise SystemExit("POLICY STOP: bootstrap %s returned NVML %d" % (name, rc))

    call("nvmlInit_v2", [])
    handle = C.c_void_p()
    call(
        "nvmlDeviceGetHandleByPciBusId_v2",
        [C.c_char_p, C.POINTER(C.c_void_p)],
        b"0000:01:00.0",
        C.byref(handle),
    )
    lower, upper = C.c_uint(), C.c_uint()
    call(
        "nvmlDeviceGetPowerManagementLimitConstraints",
        [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_uint)],
        handle,
        C.byref(lower),
        C.byref(upper),
    )
    assert storage is not None
    raise SystemExit("POLICY STOP: interception did not occur; no retry")


if __name__ == "__main__":
    main()
