"""Offline P13 (core-current policy) control-block codec and ladder rules.

Importing this file never loads a driver. Layout from the GSP control importer
(from the GSP firmware disassembly), cross-checked against the field offsets
Druta published for 595.97 (offsets and names only; no Druta code is used):
0x2080A61A GET / 0x2080E61B SET share one 6356-byte block.
"""

import struct

CONTROL_SIZE = 0x18D4
GET, SET = 0x2080A61A, 0x2080E61B
MASK_OFFSET, MASK = 0x10, 0x2000  # policy 13 only
P13_CLASS_OFFSET, P13_LIMIT_OFFSET = 0x96C, 0x970
RELATIONSHIP_MASK_OFFSET, VIOLATION_MASK_OFFSET = 0x1714, 0x1898
HEADER_SIZE = 0x10
RATED_MA = 99000
FLOOR_MA = 94000  # never below the C-1 design floor
# Policy 13 table maxima per boot-image mode; the runtime cap can never exceed these.
IMAGE_MAX_MA = {3: 99000, 4: 120000}
# A619 P13 record (base 0x80 + 13*0x168c = 0x1259c): fields that must follow a SET.


class Refused(RuntimeError):
    pass


def request():
    """A61A GET request: zero block with only the policy-13 mask set."""
    raw = bytearray(CONTROL_SIZE)
    struct.pack_into("<I", raw, MASK_OFFSET, MASK)
    return bytes(raw)


def check_control(raw, expect_limit=None):
    if len(raw) != CONTROL_SIZE:
        raise Refused("A61A control size differs")
    if struct.unpack_from("<I", raw, MASK_OFFSET)[0] != MASK:
        raise Refused("A61A policy mask is not policy 13 only")
    if raw[P13_CLASS_OFFSET] != 15:
        raise Refused("A61A policy 13 class byte is not 15 (Single1x current)")
    if (
        struct.unpack_from("<I", raw, RELATIONSHIP_MASK_OFFSET)[0]
        or struct.unpack_from("<I", raw, VIOLATION_MASK_OFFSET)[0]
    ):
        raise Refused("A61A relationship/violation masks are not zero")
    if raw[:HEADER_SIZE] == bytes(HEADER_SIZE):
        raise Refused(
            "A61A header is all zero; a SET built from it would import a zero header (C hazard)"
        )
    limit = struct.unpack_from("<I", raw, P13_LIMIT_OFFSET)[0]
    if expect_limit is not None and limit != expect_limit:
        raise Refused("A61A policy 13 limit is %d, expected %d" % (limit, expect_limit))
    return limit


def build_set(original, value_ma, cap_ma):
    """The SET block: the GET block with only +0x970 changed. Bounds are enforced here."""
    if not isinstance(value_ma, int) or not FLOOR_MA <= value_ma <= cap_ma:
        raise Refused(
            "requested %r mA outside the sitting window [%d, %d]"
            % (value_ma, FLOOR_MA, cap_ma)
        )
    check_control(original)
    raw = bytearray(original)
    struct.pack_into("<I", raw, P13_LIMIT_OFFSET, value_ma)
    return bytes(raw)


def same_except_limit(a, b):
    return (
        len(a) == len(b) == CONTROL_SIZE
        and a[:P13_LIMIT_OFFSET] == b[:P13_LIMIT_OFFSET]
        and a[P13_LIMIT_OFFSET + 4 :] == b[P13_LIMIT_OFFSET + 4 :]
    )


def status_p13(decoded):
    """Pull the P13 fields that must track a SET from a policy-status codec decode."""
    rec = next(p for p in decoded["policies"] if p["index"] == 13)
    t3 = rec["arbiters"]["gsp_type3"]
    pmu = rec["arbiters"]["pmu"]
    return dict(
        limit=rec["limit_hint_raw"],
        reading=rec["reading_hint_raw"],
        gsp_type3_output=rec["gsp_type3_output_raw"],
        gsp_type3_default=t3["default_raw"],
        gsp_type3_inputs=[(i["client_raw"], i["value_raw"]) for i in t3["inputs"]],
        pmu_output=rec["pmu_arbiter_output_raw"],
        pmu_count=pmu["count"],
        table_max=rec["maximum"],
        table_rated=rec["rated"],
    )


def verify_status(decoded, value_ma):
    """A619 acceptance after a SET or restore."""
    s = status_p13(decoded)
    problems = []
    if s["limit"] != value_ma:
        problems.append("limit %d" % s["limit"])
    if s["gsp_type3_output"] != value_ma:
        problems.append("gsp type-3 output %d" % s["gsp_type3_output"])
    if s["pmu_output"] != value_ma:
        problems.append("pmu output %d" % s["pmu_output"])
    if s["gsp_type3_default"] != RATED_MA:
        problems.append("type-3 default %d != rated" % s["gsp_type3_default"])
    if s["gsp_type3_inputs"] != [(0xFE, value_ma)]:
        problems.append("type-3 inputs %r" % (s["gsp_type3_inputs"],))
    if s["pmu_count"] != 1:
        problems.append("pmu input count %d" % s["pmu_count"])
    p2 = next(p for p in decoded["policies"] if p["index"] == 2)
    if p2["gsp_type3_output_raw"] != 125000 or p2["pmu_arbiter_output_raw"] != 125000:
        problems.append(
            "P2 outputs moved: %d/%d"
            % (p2["gsp_type3_output_raw"], p2["pmu_arbiter_output_raw"])
        )
    return s, problems


def sitting_cap(image_mode, requested_cap):
    """Cap = min(image max, sitting cap). Refuse a cap the image cannot carry rather than clamp silently."""
    if image_mode not in IMAGE_MAX_MA:
        raise Refused("unknown image mode %r" % image_mode)
    if not RATED_MA <= requested_cap <= IMAGE_MAX_MA[image_mode]:
        raise Refused(
            "sitting cap %d mA is outside [rated, image max %d] for mode %d"
            % (requested_cap, IMAGE_MAX_MA[image_mode], image_mode)
        )
    return requested_cap


# ---- the ladder schedule (pulses, null write, persistence hold, rungs), in workload seconds
# Each entry: (at_s, action, value_ma or None). 'set' writes, 'snap' is an A619 read with a label.
def ladder(cap_ma, rungs=(104000, 109000, 114000)):
    rungs = [r for r in rungs if r <= cap_ma]
    steps = []
    t = 40
    # C-1: R T R T R T R T with 20 s windows (T = 95000), snapshots mid-window
    for _ in range(4):
        steps += [
            (t, "set", 95000),
            (t + 10, "snap", "pulse"),
            (t + 20, "set", RATED_MA),
            (t + 30, "snap", "ref"),
        ]
        t += 40
    # Null arm: identity SET labelled as a treatment
    steps += [(t, "set", RATED_MA), (t + 10, "snap", "null"), (t + 20, "snap", "ref")]
    t += 40
    # UP: R U R U ... first rung held 60 s (persistence), later rungs 20 s each twice
    for i, rung in enumerate(rungs):
        hold = 60 if i == 0 else 20
        for _ in range(2):
            steps += [(t, "set", rung)]
            for k in range(10, hold, 20):
                steps.append((t + k, "snap", "up%d" % (rung // 1000)))
            steps += [(t + hold, "set", RATED_MA), (t + hold + 10, "snap", "ref")]
            t += hold + 20
    steps.append((t + 10, "snap", "final"))
    return steps, t + 20


def hold_schedule(hold_ma, cap_ma, seconds):
    """Hold mode: one whole run at a fixed P13 setting, for ABBA by FurMark score. The SET happens at
    +5 s so the whole scored run (minus its first seconds) sits at the setting; restore after the
    workload. A 99000 hold issues the identity SET so that the A and B arms have the same op count."""
    if not isinstance(hold_ma, int) or not FLOOR_MA <= hold_ma <= cap_ma:
        raise Refused(
            "hold %r mA outside the sitting window [%d, %d]"
            % (hold_ma, FLOOR_MA, cap_ma)
        )
    steps = [(5, "set", hold_ma)]
    t = 20
    while t < seconds - 10:
        steps.append((t, "snap", "hold"))
        t += 20
    steps.append((seconds - 8, "set", RATED_MA))
    steps.append((seconds - 4, "snap", "final"))
    return steps, seconds - 2
