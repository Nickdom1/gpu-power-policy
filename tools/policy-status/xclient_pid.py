#!/usr/bin/env python3
"""Read-only XRes 1.2 local-client PID census. Invoked with an external timeout.

ABI: libxres 1.2.3 XRes.h; XResQueryClientIds returns Success (0).
No X/GPU connection is opened on import. This does not depend on WM_NAME/PID.
"""

import ctypes as C
import ctypes.util
import json
from pathlib import Path
import sys

LIB = Path(ctypes.util.find_library("XRes") or "libXRes.so.1")


class Spec(C.Structure):
    _fields_ = [("client", C.c_ulong), ("mask", C.c_uint)]


class Value(C.Structure):
    _fields_ = [("spec", Spec), ("length", C.c_long), ("value", C.c_void_p)]


def bind(lib):
    signatures = {
        "XOpenDisplay": ([C.c_char_p], C.c_void_p),
        "XCloseDisplay": ([C.c_void_p], C.c_int),
        "XResQueryExtension": (
            [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)],
            C.c_int,
        ),
        "XResQueryVersion": (
            [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)],
            C.c_int,
        ),
        "XResQueryClientIds": (
            [
                C.c_void_p,
                C.c_long,
                C.POINTER(Spec),
                C.POINTER(C.c_long),
                C.POINTER(C.POINTER(Value)),
            ],
            C.c_int,
        ),
        "XResGetClientPid": ([C.POINTER(Value)], C.c_int),
        "XResClientIdsDestroy": ([C.c_long, C.POINTER(Value)], None),
    }
    for name, (args, result) in signatures.items():
        f = getattr(lib, name)
        f.argtypes = args
        f.restype = result
    return lib


def query(lib, display, window):
    spec = Spec(window, 2)  # XRES_CLIENT_ID_PID_MASK
    count = C.c_long()
    values = C.POINTER(Value)()
    if (
        lib.XResQueryClientIds(
            display, 1, C.byref(spec), C.byref(count), C.byref(values)
        )
        != 0
    ):
        raise RuntimeError("XRes client query failed")
    try:
        if not 0 <= count.value <= 256 or (count.value and not values):
            raise RuntimeError("invalid XRes reply size/pointer")
        pids = {lib.XResGetClientPid(C.byref(values[i])) for i in range(count.value)}
        pids = {pid for pid in pids if pid > 0}
        if len(pids) > 1:
            raise RuntimeError("ambiguous XRes PID")
        return next(iter(pids), None)
    finally:
        lib.XResClientIdsDestroy(count, values)


def main():
    windows = sys.argv[1:]
    if not windows or len(windows) > 256:
        raise RuntimeError("expected 1..256 window IDs")
    ids = [int(w, 16) for w in windows]
    if any(w <= 0 or w > 0xFFFFFFFF for w in ids):
        raise RuntimeError("invalid window ID")
    lib = bind(C.CDLL(str(LIB)))
    display = lib.XOpenDisplay(None)
    if not display:
        raise RuntimeError("cannot open invoking desktop X display")
    try:
        event = C.c_int()
        error = C.c_int()
        major = C.c_int()
        minor = C.c_int()
        if not lib.XResQueryExtension(display, C.byref(event), C.byref(error)):
            raise RuntimeError("XRes extension unavailable")
        if not lib.XResQueryVersion(display, C.byref(major), C.byref(minor)) or (
            major.value,
            minor.value,
        ) < (1, 2):
            raise RuntimeError("XRes 1.2 required")
        print(
            json.dumps(
                {
                    "version": [major.value, minor.value],
                    "owners": {w: query(lib, display, n) for w, n in zip(windows, ids)},
                }
            )
        )
    finally:
        lib.XCloseDisplay(display)


if __name__ == "__main__":
    main()
