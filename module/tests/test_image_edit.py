#!/usr/bin/env python3
"""CPU-only: the mode 3/4 byte edits in the two patches keep every subimage sum8 at zero.

The private board image is not available here. The test builds a synthetic compact
image with the documented pre-edit bytes at the documented offsets, balances the three
subimage checksums as the real image has them, then applies the edits exactly as the
patch text writes them (parsed from the patch, not re-typed) and checks the invariant
the kernel code enforces after editing. It also checks the delta rule that produced
the envelope checksum byte, and the decoded values.
"""

import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZE = 516608
P2_MAX, P13_RATED, P13_MAX = 0x735E8, 0x738F1, 0x738F5
SUBIMAGES = ((0, 0xFE00), (0xFE00, 0x15400), (0x15400, SIZE))


def sum8(b):
    return sum(b) & 0xFF


def synthetic():
    img = bytearray(SIZE)
    img[P2_MAX : P2_MAX + 4] = struct.pack("<I", 115000)
    img[P13_RATED : P13_RATED + 4] = struct.pack("<I", 99000)
    img[P13_MAX : P13_MAX + 4] = struct.pack("<I", 99000)
    img[SIZE - 1] = 0x63
    # Balance each subimage with one free byte, as the real image's checksums are zero.
    img[0x100] = (-sum8(img[0:0xFE00])) & 0xFF
    img[0xFE00 + 0x100] = (-sum8(img[0xFE00:0x15400])) & 0xFF
    img[0x15400 + 0x100] = (-sum8(img[0x15400:SIZE])) & 0xFF
    assert all(sum8(img[a:b]) == 0 for a, b in SUBIMAGES)
    return img


def edits_from_patch(text, mode):
    """Return [(offset, value)] for the `copy[...] = 0x..;` lines of one mode branch."""
    names = {
        "POLICY_MAX_OFFSET": P2_MAX,
        "POLICY_P13_RATED_OFFSET": P13_RATED,
        "POLICY_P13_MAX_OFFSET": P13_MAX,
        "POLICY_SIZE - 1": SIZE - 1,
    }
    # Patch lines carry a leading '+' or ' '; the branch body ends at its closing brace line.
    branch = re.search(
        r"nv_boot_policy_mode == %d\)\n[+ ]\s*\{\n(.*?)\n[+ ]\s*\}" % mode, text, re.S
    )
    assert branch, "mode %d branch missing" % mode
    edits = []
    for name, plus, value in re.findall(
        r"copy\[(\w+(?: - 1)?)(?: \+ (\d+))?\] = (0x[0-9a-f]+);", branch.group(1)
    ):
        edits.append((names[name] + int(plus or 0), int(value, 16)))
    assert edits, "no edits parsed for mode %d" % mode
    return edits


def apply(img, edits):
    out = bytearray(img)
    for off, val in edits:
        out[off] = val
    return out


def main():
    shim = (ROOT / "policy-shim.patch").read_text()
    envelope = (ROOT / "envelope-mode4.patch").read_text()
    base = synthetic()
    results = {}
    for mode, text in ((2, shim), (3, shim), (4, envelope)):
        edits = edits_from_patch(text, mode)
        edited = apply(base, edits)
        sums = [sum8(edited[a:b]) for a, b in SUBIMAGES]
        assert sums == [0, 0, 0], (mode, sums)
        delta = sum(edited[i] - base[i] for i in range(SIZE - 1))
        assert edited[SIZE - 1] == (0x63 - delta) & 0xFF, (
            "checksum byte disagrees with the delta rule"
        )
        p2 = struct.unpack_from("<I", edited, P2_MAX)[0]
        p13r = struct.unpack_from("<I", edited, P13_RATED)[0]
        p13m = struct.unpack_from("<I", edited, P13_MAX)[0]
        results[mode] = (p2, p13r, p13m, sorted(off for off, _ in edits))
    assert results[2][:3] == (120000, 99000, 99000)
    assert results[3][:3] == (125000, 99000, 99000)
    assert results[4][:3] == (140000, 99000, 120000), results[4]
    # The envelope touches exactly: three P2 maximum bytes, two P13 maximum bytes, the checksum.
    assert results[4][3] == [
        P2_MAX,
        P2_MAX + 1,
        P2_MAX + 2,
        P13_MAX,
        P13_MAX + 1,
        SIZE - 1,
    ], results[4][3]
    # Mode 3's edit set is the reviewed two-byte pattern plus checksum.
    assert results[3][3] == [P2_MAX, P2_MAX + 1, SIZE - 1]
    # The envelope patch adds pre-edit guards for the P13 words and leaves the P2 guard.
    assert (
        r'memcmp(copy + POLICY_P13_RATED_OFFSET, "\xb8\x82\x01\x00", 4) != 0'
        in envelope
    )
    assert (
        r'memcmp(copy + POLICY_P13_MAX_OFFSET, "\xb8\x82\x01\x00", 4) != 0' in envelope
    )
    assert "copy[POLICY_P13_RATED_OFFSET] =" not in envelope, (
        "the envelope must not change the P13 rated word"
    )
    assert "copy[POLICY_MAX_OFFSET + 3]" not in envelope
    print(
        "image-edit cases PASS: modes 2/3/4 keep sum8 == 0, envelope = 140000 mW / 120000 mA, rated untouched"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
