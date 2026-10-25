#!/usr/bin/env python3
"""Root-run long-lived RM client inferior; always run under client.gdb.

Loads the pinned NVML once, then serves operations from the op directory:
op-N.json {"kind": "info"|"get"|"status"|"set"|"quit", "request_hex": ...}.
Each operation calls the public power-limit setter with 125000 mW; the GDB hook
replaces that one RM request (command, buffer, size) with the operation's
command and this process's guarded buffer, then records the RM status. Without
the hook the setter would simply send an ordinary 125 W request to policy 2.
CPU tests use a synthetic library in tests/fixture.s instead of NVML.
"""

import ctypes as C
import hashlib
import json
import os
import sys
import time
from pathlib import Path

CANARY = bytes.fromhex("5054353136502d3133") * 2  # 'PT516P-13' twice
COMMANDS = {
    "info": (0x2080A618, 0x21B8),
    "get": (0x2080A61A, 0x18D4),
    "status": (0x2080A619, 0x2E740),
    "set": (0x2080E61B, 0x18D4),
}


def save(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def main():
    ops = Path(os.environ["PT516_P13_OPS"])
    library = Path(
        os.environ.get("PT516_P13_LIBRARY", "/run/opengl-driver/lib/libnvidia-ml.so.1")
    )
    expected = os.environ["PT516_P13_LIBRARY_SHA256"]
    with library.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != expected:
            raise SystemExit("P13 STOP: NVML hash differs before loading")
    pci = Path("/sys/bus/pci/devices/0000:01:00.0")
    if os.environ.get("PT516_P13_FIXTURE") != "1":
        if (pci / "vendor").read_text().strip() != "0x10de" or (
            pci / "device"
        ).read_text().strip() != "0x2460":
            raise SystemExit("P13 STOP: PCI identity differs")
    lib = C.CDLL(str(library))
    if os.environ.get("PT516_P13_FIXTURE") == "1":
        # CPU tests only: the synthetic library serves A618/A61A/A619 from these buffers.
        fixture_dir = Path(os.environ["PT516_P13_FIXTURE_DIR"])
        keep = []
        for name, symbol in (
            ("info.bin", "fixture_info"),
            ("control.bin", "fixture_control"),
            ("status.bin", "fixture_status"),
        ):
            data = (fixture_dir / name).read_bytes()
            buf = C.create_string_buffer(data, len(data))
            keep.append(buf)
            C.c_void_p.in_dll(lib, symbol).value = C.addressof(buf)
        C.c_uint.in_dll(lib, "fixture_max").value = int(
            os.environ.get("PT516_P13_FIXTURE_MAX", "99000")
        )

    def call(name, types, *args):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = types, C.c_int
        return fn(*args)

    if call("nvmlInit_v2", []):
        raise SystemExit("P13 STOP: nvmlInit failed")
    handle = C.c_void_p()
    if call(
        "nvmlDeviceGetHandleByPciBusId_v2",
        [C.c_char_p, C.POINTER(C.c_void_p)],
        b"0000:01:00.0",
        C.byref(handle),
    ):
        raise SystemExit("P13 STOP: device handle unavailable")
    save(
        ops / "client-ready.json", json.dumps(dict(pid=os.getpid(), epoch=time.time()))
    )
    n = 0
    deadline = time.monotonic() + 900
    try:
        while True:
            path = ops / ("op-%04d.json" % n)
            if not path.exists():
                if time.monotonic() > deadline:
                    raise SystemExit(
                        "P13 STOP: client idle timeout; no further operations"
                    )
                time.sleep(0.02)
                continue
            op = json.loads(path.read_text())
            deadline = time.monotonic() + 900
            if op["kind"] == "quit":
                save(
                    ops / ("op-%04d.result.json" % n),
                    json.dumps(dict(kind="quit", ok=True)),
                )
                return 0
            command, size = COMMANDS[op["kind"]]
            request = bytes.fromhex(op["request_hex"])
            if len(request) != size:
                raise SystemExit("P13 STOP: op %d request size differs" % n)
            storage = C.create_string_buffer(CANARY + request + CANARY)
            pointer = C.addressof(storage) + len(CANARY)
            save(
                ops / ("op-%04d.buffer.json" % n),
                json.dumps(
                    dict(
                        n=n,
                        pid=os.getpid(),
                        kind=op["kind"],
                        command=command,
                        size=size,
                        pointer=pointer,
                        request_sha256=hashlib.sha256(request).hexdigest(),
                    )
                ),
            )
            save(ops / "current-op", str(n))
            started = time.time()
            rc = call(
                "nvmlDeviceSetPowerManagementLimit",
                [C.c_void_p, C.c_uint],
                handle,
                C.c_uint(125000),
            )
            ended = time.time()
            rm_path = ops / ("op-%04d.rm.json" % n)
            if not rm_path.exists():
                raise SystemExit(
                    "P13 STOP: the hook did not record op %d; the public setter may have run unmodified"
                    % n
                )
            rm = json.loads(rm_path.read_text())
            reply = storage.raw[len(CANARY) : len(CANARY) + size]
            guards = (
                storage.raw[: len(CANARY)] == CANARY
                and storage.raw[len(CANARY) + size : len(CANARY) * 2 + size] == CANARY
            )
            save(ops / ("op-%04d.reply.bin" % n), reply)
            save(
                ops / ("op-%04d.result.json" % n),
                json.dumps(
                    dict(
                        n=n,
                        kind=op["kind"],
                        command=command,
                        size=size,
                        rm_status=rm["status"],
                        nvml_rc=rc,
                        guards_intact=guards,
                        started_epoch=started,
                        ended_epoch=ended,
                        reply_sha256=hashlib.sha256(reply).hexdigest(),
                    )
                ),
            )
            if not guards:
                raise SystemExit(
                    "P13 STOP: buffer guard changed on op %d; reply retained" % n
                )
            n += 1
    finally:
        call("nvmlShutdown", [])


if __name__ == "__main__":
    sys.exit(main())
