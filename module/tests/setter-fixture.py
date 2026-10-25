import ctypes as C
import struct
import sys

lib = C.CDLL(sys.argv[1])
case = sys.argv[2]
mem = C.create_string_buffer(0x26000)
struct.pack_into("<III", mem, 0x2306C, 1, 0, 0)
base = 0x23078
struct.pack_into("<I", mem, base + 4, 0x7F21F)
mem[base + 0x28] = b"\xff"
mem[base + 0x2A] = b"\x02"
mem[base + 0x44 : base + 0x57] = bytes(
    [255, 255, 255, 2, 15, 14, 255, 0, 2] + [255] * 10
)
struct.pack_into("<III", mem, base + 0x60 + 2 * 0xE4, 1000, 80000, 125000)
if case == "old-max":
    struct.pack_into("<I", mem, base + 0x68 + 2 * 0xE4, 115000)
if case == "previous-max":
    struct.pack_into("<I", mem, base + 0x68 + 2 * 0xE4, 120000)
if case in ("envelope-max", "envelope-unexpected"):
    struct.pack_into("<I", mem, base + 0x68 + 2 * 0xE4, 140000)
if case == "role":
    mem[base + 0x44 + 8] = b"\xff"
if case == "cache":
    struct.pack_into("<I", mem, 0x2306C, 0)
if case == "type":
    mem[base + 0x5C + 2 * 0xE4] = b"\x01"
if case == "mask":
    struct.pack_into("<I", mem, base + 4, 0)
if case == "packet":
    (C.c_ubyte * 12).in_dll(lib, "fixture_packet")[1] = 0xFD
if case == "size":
    C.c_uint.in_dll(lib, "fixture_size").value = 11
if case == "command":
    C.c_uint.in_dll(lib, "fixture_command").value = 0x2080E63A
if case == "rejected":
    C.c_uint.in_dll(lib, "fixture_status").value = 0x1F
if case == "v2":
    lib.nvmlDeviceSetPowerManagementLimit_v2()
value = 115000 if case == "value" else 125000
lib.nvmlDeviceSetPowerManagementLimit(C.byref(mem), C.c_uint(value))
assert C.c_uint.in_dll(lib, "fixture_count").value == 1
assert bytes((C.c_ubyte * 12).in_dll(lib, "fixture_after")) == struct.pack(
    "<BBHII", 8, 254, 0, 125000, 0
)
print("FIXTURE verified exactly one altered-role packet", flush=True)
if case == "repeat":
    lib.nvmlDeviceSetPowerManagementLimit(C.byref(mem), C.c_uint(value))
