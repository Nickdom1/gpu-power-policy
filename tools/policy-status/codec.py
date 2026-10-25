"""Offline pinned-policy decoder. No driver imports or hardware operations."""

import hashlib
import struct

EXPECTED = "5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181"
CANARY = b"PT516-POLICY-RO!!"
INFO_SIZE, STATUS_SIZE = 0x21B8, 0x2E740
MASK = 0x7F21F
TYPES = {
    0: 16,
    1: 3,
    2: 0,
    3: 15,
    4: 15,
    9: 4,
    12: 16,
    13: 15,
    14: 16,
    15: 16,
    16: 4,
    17: 4,
    18: 15,
}
CURRENT = {9, 12, 13, 18}
# Policy 2 / policy 13 table maxima per boot-image mode (mode 3 default, mode 4 envelope).
IMAGE_MAXIMA = {3: (125000, 99000), 4: (140000, 120000)}
QUERIES = {"info": (0x2080A618, INFO_SIZE), "status": (0x2080A619, STATUS_SIZE)}


def u32(raw, off):
    return struct.unpack_from("<I", raw, off)[0]


def inventory(raw):
    if len(raw) != INFO_SIZE:
        raise ValueError("A618 size differs")
    mask = u32(raw, 4)
    if mask != MASK:
        raise ValueError("A618 policy inventory differs from pinned board")
    rows = []
    maxima = {}
    for i, kind in TYPES.items():
        off = 0x58 + i * 0xE4
        typ, channel, unit = raw[off + 4 : off + 7]
        if typ != kind or unit != int(i in CURRENT):
            raise ValueError("A618 policy type/unit differs: %d" % i)
        low, rated, high = struct.unpack_from("<III", raw, off + 8)
        if not low <= rated <= high:
            raise ValueError("A618 bounds inconsistent: %d" % i)
        if i == 2 and (channel, low, rated) != (0, 1000, 80000):
            raise ValueError("fresh policy 2 minimum/rated differ from the board")
        if i == 13 and (channel, low, rated) != (19, 1, 99000):
            raise ValueError(
                "fresh policy 13 minimum/rated differ from the 99 A inventory"
            )
        if i in (2, 13):
            maxima[i] = high
        extra = {}
        if i == 2:
            w = u32(raw, off + 32)
            extra = dict(
                controller_past_samples_raw=w & 255,
                controller_next_samples_raw=(w >> 8) & 255,
                controller_ratio_min_raw=w >> 16,
                controller_ratio_max_raw=u32(raw, off + 36) & 65535,
            )
        rows.append(
            dict(
                extra,
                index=i,
                type=typ,
                channel=channel,
                unit_raw=unit,
                unit="mA" if unit else "mW",
                minimum=low,
                rated=rated,
                maximum=high,
            )
        )
    mode = {v: k for k, v in IMAGE_MAXIMA.items()}.get((maxima[2], maxima[13]))
    if mode is None:
        raise ValueError(
            "fresh policy 2/13 maxima %r match neither the 125 W image nor the envelope"
            % (maxima,)
        )
    return dict(
        mask=mask,
        sha256=hashlib.sha256(raw).hexdigest(),
        image_mode=mode,
        policies=rows,
    )


def request(query, info=None):
    command, size = QUERIES[query]
    raw = bytearray(size)
    if query == "status":
        struct.pack_into("<I", raw, 4, inventory(info)["mask"])
    return command, bytes(raw)


def status(raw, info):
    inv = inventory(info)
    if len(raw) != STATUS_SIZE or u32(raw, 4) != inv["mask"]:
        raise ValueError("A619 size or echoed policy mask differs")
    rows = []
    for policy in inv["policies"]:
        off = 0x80 + policy["index"] * 0x168C
        if raw[off] != policy["type"]:
            raise ValueError("A619 class differs from fresh A618: %d" % policy["index"])
        # The +4/+8 field names follow Druta's published offsets (facts, not code);
        # raw words and complete replies are retained. Arbiter labels come from GSP export tracing, not live proof.
        rows.append(
            dict(
                policy,
                **extended(raw, off, policy),
                record_offset=off,
                status_class_raw=raw[off],
                limit_hint_raw=u32(raw, off + 4),
                reading_hint_raw=u32(raw, off + 8),
                gsp_type3_output_raw=u32(raw, off + 28),
                gsp_type6_output_raw=u32(raw, off + 104),
                pmu_arbiter_output_raw=u32(raw, off + 180),
                pmu_input_count_raw=raw[off + 177],
                prefix_252_hex=raw[off : off + 252].hex(),
            )
        )
    return dict(
        mask=inv["mask"],
        inventory_sha256=inv["sha256"],
        reply_sha256=hashlib.sha256(raw).hexdigest(),
        policies=rows,
        header_128_hex=raw[:128].hex(),
        domain_header_raw=list(struct.unpack_from("<III", raw, 8)),
        clock_comparison=compare_clocks(raw, rows),
        interpretation="Status snapshot; freshness and unique winning regulator are not established. "
        "record+4/+8 limit/reading names follow Druta's published offsets; retain raw units.",
    )


def arbiter(raw, off):
    count = raw[off + 1]
    if count > 8:
        raise ValueError("arbiter input count exceeds pinned eight-slot layout")
    return dict(
        byte0_raw=raw[off],
        count=count,
        output_raw=u32(raw, off + 4),
        default_raw=u32(raw, off + 8),
        inputs=[
            dict(client_raw=raw[off + 12 + 8 * k], value_raw=u32(raw, off + 16 + 8 * k))
            for k in range(count)
        ],
    )


def extended(raw, off, policy):
    # Pinned GSP exporters: base 16ff240, header 1701dbc, domain 17295b8,
    # Combined1x 172f310/172f598. The PMU algorithms are not decoded here.
    d = dict(
        base16_signed_raw=struct.unpack_from("<i", raw, off + 16)[0],
        base20_raw=u32(raw, off + 20),
        arbiters={
            name: arbiter(raw, off + offset)
            for name, offset in [("gsp_type3", 24), ("gsp_type6", 100), ("pmu", 176)]
        },
    )
    if policy["type"] == 16:
        d.update(
            domain_limits_raw=list(struct.unpack_from("<III", raw, off + 252)),
            domain_ceilings_raw=list(struct.unpack_from("<III", raw, off + 264)),
            domain_flag276_raw=raw[off + 276],
        )
    return d


def compare_clocks(raw, policies):
    candidates = {
        r["index"]: r["domain_limits_raw"][1:]
        for r in policies
        if r["index"] in (0, 12, 14, 15)
    }
    header = list(struct.unpack_from("<II", raw, 12))
    minimum = [min(v[k] for v in candidates.values()) for k in range(2)]
    return dict(
        header_khz_hint=header,
        policy_khz_hints=candidates,
        elementwise_min_khz_hint=minimum,
        header_equals_min=header == minimum,
        exact_matches=[
            [i for i, v in candidates.items() if v[k] == header[k]] for k in range(2)
        ],
        within_1mhz=[
            [i for i, v in candidates.items() if abs(v[k] - header[k]) <= 1000]
            for k in range(2)
        ],
        interpretation="Numerical equality only; clock units/domain identities are inferred. "
        "Near matches and estimator ranks do not establish a unique active limiter.",
    )
