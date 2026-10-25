"""Synthetic CPU library only. Not the production bootstrap."""

import ctypes as C
import json
import os
from pathlib import Path
import struct
import sys

sys.path.insert(0, os.environ["PT516_POLICY_LAB"])
from codec import CANARY, request

mode = sys.argv[2]
out = Path(os.environ["PT516_POLICY_DIR"])
query = os.environ["PT516_POLICY_QUERY"]
info = (out.parent / "info/a618.bin").read_bytes() if query == "status" else None
_, data = request(query, info)
size = len(data)
storage = C.create_string_buffer(CANARY + data + CANARY)
source = C.create_string_buffer(0x2210)
desc = dict(pid=os.getpid(), pointer=C.addressof(storage) + len(CANARY), size=size)
if mode == "pid":
    desc["pid"] += 1
elif mode == "buffer-size":
    desc["size"] -= 1
elif mode == "pointer":
    desc["pointer"] = 0
elif mode == "overlap":
    desc["pointer"] = C.addressof(source)
elif mode == "pristine":
    storage[len(CANARY)] = b"\x01"
elif mode == "pre-guard":
    storage[0] = b"\x00"
(out / "buffer.json").write_text(json.dumps(desc))
payload = C.create_string_buffer(Path(sys.argv[3]).read_bytes())
lib = C.CDLL(sys.argv[1])
C.c_void_p.in_dll(lib, "fixture_payload").value = C.addressof(payload)
cmd = (
    0x2080E63A
    if mode == "privileged"
    else (0x2080A620 if mode == "missing" else 0x2080A618)
)
struct.pack_into(
    "<IIII",
    source,
    0x2200,
    cmd,
    0x20 if mode == "size" else 0x21B8,
    0x56 if mode == "unsupported" else 0,
    {"callback": 1, "caller": 2, "post-guard": 3, "source-mutated": 4}.get(mode, 0),
)
if mode in ("setter", "v2"):
    getattr(
        lib, "nvmlDeviceSetPowerManagementLimit" + ("_v2" if mode == "v2" else "")
    )()
lib.nvmlDeviceGetPowerManagementLimitConstraints(C.byref(source))
raise SystemExit("FIXTURE unexpectedly returned")
