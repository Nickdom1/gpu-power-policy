"""615 policy buffers, checked against the live captures in tests/fixtures."""
import struct

INFO, GET, SET, STATUS = 0x2080A618, 0x2080A61A, 0x2080E61B, 0x2080A619
SIZES = {INFO: 0x5020, GET: 0x3634, SET: 0x3634, STATUS: 0x60EF8}
# Verified by read-only 615.71.09 captures, including the single-policy request.
CURRENT_LAYOUT_VERIFIED = True


def u32(raw, off):
    return struct.unpack_from("<I", raw, off)[0]


def checked(command, raw):
    if len(raw) != SIZES[command]:
        raise ValueError(f"0x{command:08x}: reply size {len(raw)} != {SIZES[command]}")
    return raw


def query(rm, command, mask=0):
    raw = bytearray(SIZES[command])
    if command != INFO:
        struct.pack_into("<I", raw, 0x10 if command == GET else 4, mask)
    _, reply = rm.control(command, raw)
    checked(command, reply)
    if command != INFO and u32(reply, 0x10 if command == GET else 4) != mask:
        raise ValueError("RM did not echo the requested policy mask")
    return reply


def table(command, raw):
    checked(command, raw)
    mask = u32(raw, 4 if command == INFO else 0x10)
    rows = []
    for index in range(32):
        if mask & (1 << index):
            off = 0xCC + index * 0x104 if command == INFO else 0x14 + index * 0xC4
            if command == INFO:
                rows.append(dict(index=index, cls=raw[off + 4],
                                 words=list(struct.unpack_from("<4I", raw, off + 8))))
            else:
                rows.append(dict(index=index, cls=raw[off], limit=u32(raw, off + 4)))
    return rows


def current_policy(info, control, rated, ceiling):
    candidates = [r for r in table(INFO, info) if r["cls"] == 15 and r["words"][1] == rated]
    if len(candidates) != 1:
        raise ValueError("current policy class/rated value is not unique")
    row = candidates[0]
    low, live_rated, high, _ = row["words"]
    if not 0 < low <= live_rated <= high:
        raise ValueError("A618 current bounds are inconsistent")
    lower, upper = max(rated, low), min(ceiling, high)
    matches = [r for r in table(GET, control) if r["index"] == row["index"] and r["cls"] == 15]
    if len(matches) != 1 or not lower <= matches[0]["limit"] <= upper:
        raise ValueError("A61A current policy does not match A618/image bounds")
    return row["index"], matches[0]["limit"], lower, upper


def set_current(rm, info, rated, ceiling, value):
    if not CURRENT_LAYOUT_VERIFIED:
        raise ValueError("current SET disabled: capture gpu-power dump and verify the live 615 layout first")
    control = query(rm, GET, u32(info, 4))
    index, _, lower, upper = current_policy(info, control, rated, ceiling)
    if not lower <= value <= upper:
        raise ValueError(f"current must be within {lower}..{upper} mA")
    control = query(rm, GET, 1 << index)
    current_policy(info, control, rated, ceiling)
    if control[:16] != bytes(12) + bytes.fromhex("ff000000") or u32(control, 0x3114) or u32(control, 0x3298):
        raise ValueError("unexpected A61A header or relationship/violation masks")
    changed = bytearray(control)
    struct.pack_into("<I", changed, 0x18 + index * 0xC4, value)
    _, reply = rm.control(SET, changed)
    checked(SET, reply)
    readback = query(rm, GET, 1 << index)
    _, actual, _, _ = current_policy(info, readback, rated, ceiling)
    if actual != value:
        raise RuntimeError(f"current SET readback {actual} != {value} mA; inspect status")
    return actual
