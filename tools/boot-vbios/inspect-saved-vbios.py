#!/usr/bin/env python3
"""Offline inspection of the saved boot capture; never opens GPU interfaces.

The policy decoder deliberately accepts only the observed v3/44/71 layout.
It does not modify images, authenticate signatures or establish flashability.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct

MAX_IMAGE = 1024 * 1024
H07_ROLES = [
    255,
    255,
    255,
    2,
    15,
    14,
    255,
    0,
    2,
    255,
    255,
    255,
    255,
    255,
    255,
    255,
    255,
    255,
    255,
]
H07_VALUES = {
    0: (1, 5001000, 5001000),
    1: (1, 1001000, 1001000),
    2: (1000, 80000, 115000),
    3: (1, 5001000, 5001000),
    4: (1, 5001000, 5001000),
    9: (1000, 8000, 8000),
    12: (1000, 5001000, 5001000),
    13: (1, 99000, 99000),
    14: (1, 5001000, 5001000),
    15: (1, 5001000, 5001000),
    16: (10000, 37500, 37500),
    17: (1, 250000, 250000),
    18: (1, 5001000, 5001000),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def part(data, offset, size):
    require(
        offset >= 0 and size >= 0 and offset + size <= len(data),
        f"out-of-bounds span {offset:#x}+{size:#x}",
    )
    return data[offset : offset + size]


def u16(data, offset):
    return struct.unpack("<H", part(data, offset, 2))[0]


def u32(data, offset):
    return struct.unpack("<I", part(data, offset, 4))[0]


def parse_chain(data):
    require(0 < len(data) <= MAX_IMAGE, "image size outside bounds")
    result, offset = [], 0
    for index in range(32):
        sig = u16(data, offset)
        require(sig in (0xAA55, 0x4E56, 0xBB77), "invalid expansion ROM signature")
        pcir = offset + u16(data, offset + 0x18)
        signature = part(data, pcir, 4)
        require(signature in (b"PCIR", b"NPDS", b"RGIS"), "invalid PCI data signature")
        pcir_size = u16(data, pcir + 10)
        require(pcir_size >= 24, "short PCI data structure")
        part(data, pcir, pcir_size)
        image_size = u16(data, pcir + 16) * 512
        sub_size = image_size
        last = bool(part(data, pcir + 21, 1)[0] & 128)
        item = dict(
            index=index,
            offset=offset,
            signature=hex(sig),
            pcir_offset=pcir,
            pcir_signature=signature.decode("ascii"),
            vendor=hex(u16(data, pcir + 4)),
            device=hex(u16(data, pcir + 6)),
            code_type=part(data, pcir + 20, 1)[0],
            pcir_image_bytes=image_size,
            pcir_last=last,
        )
        ext = (pcir + pcir_size + 15) & ~15
        if part(data, ext, 4) == b"NPDE":
            version, size = u16(data, ext + 4), u16(data, ext + 6)
            require(version in (0x100, 0x101) and size >= 10, "unsupported NPDE layout")
            part(data, ext, size)
            sub_size = u16(data, ext + 8) * 512
            if size >= 11:
                last = bool(data[ext + 10] & 128)
            elif sub_size < image_size:
                last = False
            item["npde"] = dict(
                offset=ext,
                version=hex(version),
                size=size,
                flags=data[ext + 11] if size >= 12 else None,
            )
            require(ext + size <= offset + sub_size, "NPDE outside its subimage")
        require(
            sub_size > 0 and pcir + pcir_size <= offset + sub_size,
            "invalid subimage extent",
        )
        block = part(data, offset, sub_size)
        item.update(
            subimage_bytes=sub_size,
            end=offset + sub_size,
            effective_last=last,
            sum8=sum(block) & 255,
        )
        result.append(item)
        offset += sub_size
        if last:
            require(
                offset == len(data), "trailing bytes outside the declared ROM chain"
            )
            return result
    raise ValueError("no terminating ROM within 32 images")


def parse_bit(data, base_size, relocation):
    candidates, invalid = [], []
    offset = data.find(b"\xff\xb8BIT\0")
    while offset >= 0:
        try:
            require(u16(data, offset + 6) == 0x100, "unsupported BIT version")
            header = part(data, offset, 12)
            hs, ts, count = header[8:11]
            require(hs >= 12 and ts in (6, 7, 8), "unsupported BIT header/token size")
            require(sum(part(data, offset, hs)) & 255 == 0, "BIT checksum mismatch")
            part(data, offset + hs, ts * count)
            tokens = []
            for index in range(count):
                pos = offset + hs + index * ts
                ident, version = data[pos : pos + 2]
                size = u16(data, pos + 2)
                raw = u16(data, pos + 4) if ts < 8 else u32(data, pos + 4)
                target = raw + relocation if raw > base_size else raw
                if raw:
                    part(data, target, size)
                else:
                    require(size == 0, "null token with nonzero size")
                tokens.append(
                    dict(
                        id=chr(ident),
                        version=version,
                        size=size,
                        raw_pointer=raw,
                        offset=target,
                    )
                )
            candidates.append(
                dict(
                    offset=offset,
                    header_size=hs,
                    token_size=ts,
                    checksum8=0,
                    tokens=tokens,
                )
            )
        except ValueError as exc:
            invalid.append(dict(offset=offset, error=str(exc)))
        offset = data.find(b"\xff\xb8BIT\0", offset + 1)
    require(len(candidates) == 1, f"expected one valid BIT, found {len(candidates)}")
    return candidates[0], invalid


def analyze(data, recorded_expansion_offset):
    chain = parse_chain(data)
    base = next((x for x in chain if x["code_type"] == 0), None)
    ext = next((x for x in chain if x["code_type"] == 0xE0), None)
    require(base is not None and base["offset"] == 0, "missing initial legacy image")
    require(
        (base["vendor"], base["device"]) == ("0x10de", "0x2460"),
        "wrong base PCI identity",
    )
    relocation = ext["offset"] - base["subimage_bytes"] if ext else 0
    require(
        relocation == recorded_expansion_offset,
        "snapshot relocation differs from ROM chain",
    )
    bit, invalid = parse_bit(data, base["subimage_bytes"], relocation)
    perf = [x for x in bit["tokens"] if x["id"] == "P"]
    bios = [x for x in bit["tokens"] if x["id"] == "B"]
    require(len(perf) == 1 and perf[0]["version"] == 2, "expected one BIT P version 2")
    perf = perf[0]
    payload = part(data, perf["offset"], perf["size"])
    raw = u32(payload, 0x2C)
    require(raw != 0, "no power-policy pointer")
    table = raw + relocation if raw > base["subimage_bytes"] else raw
    version, hs, es, count = part(data, table, 4)
    require(
        (version, hs, es) == (0x30, 44, 71) and count <= 32,
        "unsupported policy layout; inspect version/header/entry sizes before decoding",
    )
    header = part(data, table, hs)
    part(data, table + hs, es * count)
    roles = [255] * 19
    # Actual v3/44-byte descriptor and stores at 11a1e20..11a207a, 11a2c04.
    for role, off in {
        0: 11,
        1: 10,
        2: 12,
        3: 13,
        4: 31,
        5: 32,
        6: 33,
        7: 36,
        8: 38,
        9: 39,
    }.items():
        roles[role] = header[off]
    entries, mask = [], 0
    for index in range(count):
        pos = table + hs + index * es
        entry = part(data, pos, es)
        # Descriptor 2b8d3b2w1b6d1b1d; upper type byte is entry[66].
        kind = ((entry[0] & 15) | (entry[66] << 4)) & 255
        included = kind not in (0, 7)  # exact early skip at 11a215e / 11a2164
        if included:
            mask |= 1 << index
        values = [u32(entry, off) for off in (2, 6, 10)]
        entries.append(
            dict(
                index=index,
                offset=pos,
                raw_type=kind,
                passes_initial_type_filter=included,
                min_rated_max_raw=values,
                prefix14_hex=entry[:14].hex(),
                type_high_byte_at_66=entry[66],
            )
        )
    require(len(entries) > 2, "missing board policy entry 2")
    flags = header[37]
    seed_present = len(payload) >= 248
    seed = dict(
        field_offset=0xF4,
        physically_present=seed_present,
        raw_value=u32(payload, 0xF4) if seed_present else None,
        parser_consumes_field=len(payload) >= 252,
        interpretation="Field absent"
        if not seed_present
        else "See length-selected parser descriptor",
    )
    if len(payload) == 232:
        seed.update(parser_descriptor="58d", parser_output_field_value=0)
    result = dict(
        image_bytes=len(data),
        image_sha256=hashlib.sha256(data).hexdigest(),
        declared_rom_chain_complete=True,
        all_subimage_checksums_zero=all(x["sum8"] == 0 for x in chain),
        cryptographic_signature_verified=False,
        complete_flash_device_dump=False,
        resident_gsp_identity_verified=False,
        chain=chain,
        relocation_bytes=relocation,
        bit=bit,
        rejected_bit_candidates=invalid,
        perf_token=perf,
        stored_action_pointer=seed,
        policy=dict(
            raw_pointer=raw,
            offset=table,
            version=hex(version),
            header_size=hs,
            entry_size=es,
            entry_count=count,
            header_hex=header.hex(),
            role_to_policy=roles,
            flags_byte_offset=37,
            flags_byte=flags,
            flag_bit0=flags & 1,
            fd_raise_gate_bit1=(flags >> 1) & 1,
            initial_type_filter_mask=hex(mask),
            entries=entries,
        ),
        h07_comparison=dict(
            roles_equal=roles == H07_ROLES,
            mask_equal=mask == 0x7F21F,
            all_13_raw_triples_equal=all(
                i < len(entries) and tuple(entries[i]["min_rated_max_raw"]) == v
                for i, v in H07_VALUES.items()
            ),
        ),
    )
    if len(bios) == 1 and bios[0]["size"] >= 5:
        pos = bios[0]["offset"]
        version = u32(data, pos)
        result["bios_version"] = (
            ".".join(f"{(version >> shift) & 255:02X}" for shift in (24, 16, 8, 0))
            + f".{data[pos + 4]:02X}"
        )
    return result


def read_local(path, limit):
    path = path.resolve(strict=True)
    require(
        path.parts[1:2] not in [("proc",), ("sys",), ("dev",)],
        "only saved local files are accepted",
    )
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        require(stat.S_ISREG(os.fstat(f.fileno()).st_mode), "not a regular saved file")
        data = f.read(limit + 1)
    require(len(data) <= limit, "saved file exceeds bound")
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture_directory", type=Path)
    a = p.parse_args()
    meta = json.loads(read_local(a.capture_directory / "capture.json", 16384))
    snap = read_local(a.capture_directory / "snapshot.bin", MAX_IMAGE + 64)
    image = read_local(a.capture_directory / "flash-copy.bin", MAX_IMAGE)
    # Importing the collector defines its pure decoder; it does not run main().
    path = Path(__file__).with_name("collect-boot-vbios.py")
    spec = importlib.util.spec_from_file_location("boot_collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    decoded = module.decode(snap)
    require(
        decoded["state"] == "captured" and snap[64:] == image,
        "snapshot/payload mismatch",
    )
    require(
        meta.get("complete_read") is True and meta.get("state") == "captured",
        "metadata says capture incomplete",
    )
    for key in [
        "snapshot_sha256",
        "image_sha256",
        "image_bytes",
        "expansion_rom_offset",
    ]:
        require(meta[key] == decoded[key], "metadata mismatch: " + key)
    result = analyze(image, decoded["expansion_rom_offset"])
    result["capture_utc"] = meta["utc"]
    result["snapshot_sha256"] = decoded["snapshot_sha256"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
