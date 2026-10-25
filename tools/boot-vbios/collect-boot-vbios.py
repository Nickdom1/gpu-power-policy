#!/usr/bin/env python3
"""Read one retained boot VBIOS snapshot from the experimental driver.

Does not initialize the GPU, read PCI ROM, issue RM requests, retry, or change
power. The experimental module must already be booted. --decode reads a local
snapshot instead and does not touch procfs/sysfs. Image completeness remains
unverified until the captured NVIDIA ROM chain is analyzed offline.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile

NODE = Path("/proc/driver/nvidia/gpus/0000:01:00.0/boot_vbios")
MAX_IMAGE = 1024 * 1024
HEADER_SIZE = 64
STATES = {
    0: "not_reached",
    1: "captured",
    2: "extraction_failed",
    3: "invalid_image",
    4: "retention_allocation_failed",
}
# The shipped modules' srcversions, pinned in module/provenance.json (mode 3 and the envelope).
PROVENANCE = Path(__file__).resolve().parents[2] / "module" / "provenance.json"


def expected_srcversions():
    try:
        pin = json.loads(PROVENANCE.read_text())
    except (OSError, ValueError):
        return ()
    return tuple(
        v
        for v in (pin.get("module_srcversion"), pin.get("envelope_module_srcversion"))
        if v
    )


def decode(blob):
    if len(blob) < HEADER_SIZE or len(blob) > HEADER_SIZE + MAX_IMAGE:
        raise ValueError("snapshot size outside format bounds")
    if blob[:8] != b"NVBROM1\0":
        raise ValueError("wrong snapshot magic")
    fields = struct.unpack_from("<14I", blob, 8)
    (
        version,
        header,
        state,
        rm,
        size,
        offset,
        domain,
        bus,
        slot,
        func,
        vendor,
        device,
        r0,
        r1,
    ) = fields
    if version != 1 or header != HEADER_SIZE or state not in STATES or r0 or r1:
        raise ValueError("unsupported or malformed snapshot header")
    if (domain, bus, slot, func, vendor, device) != (0, 1, 0, 0, 0x10DE, 0x2460):
        raise ValueError("snapshot is not the pinned 0000:01:00.0 NVIDIA 2460 GPU")
    if len(blob) != HEADER_SIZE + size or size > MAX_IMAGE or offset > size:
        raise ValueError("payload length/offset mismatch")
    if state == 1:
        if rm != 0 or size == 0:
            raise ValueError("captured state has invalid status/size")
    elif size != 0 or offset != 0:
        raise ValueError("non-captured state unexpectedly contains image bytes")
    result = {
        "format_version": version,
        "state": STATES[state],
        "rm_extraction_status": hex(rm),
        "image_bytes": size,
        "expansion_rom_offset": offset,
        "pci": "0000:01:00.0",
        "device": "0x2460",
        "snapshot_sha256": hashlib.sha256(blob).hexdigest(),
        "full_nvidia_image_verified": False,
        "source_kind": "host flash copy from existing boot extraction, before FWSEC parsing",
    }
    if state == 1:
        image = blob[HEADER_SIZE:]
        result.update(
            image_sha256=hashlib.sha256(image).hexdigest(),
            starts_55aa=image[:2] == b"\x55\xaa",
        )
    return result


def read_bounded(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("source is not a regular proc/local file")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        after = os.fstat(source.fileno())
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise ValueError("source changed during open")
        return source.read(HEADER_SIZE + MAX_IMAGE + 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--decode",
        type=Path,
        help="decode a saved local snapshot, without GPU interfaces",
    )
    p.add_argument(
        "--output-dir", type=Path, help="new private directory; never overwrite"
    )
    a = p.parse_args()
    if a.decode and a.decode.resolve().parts[1:2] in [("proc",), ("sys",), ("dev",)]:
        p.error("--decode requires a saved local file outside /proc, /sys and /dev")
    if a.decode is None and os.geteuid() != 0:
        p.error("capture requires root to read the experimental diagnostic node")
    os.umask(0o077)
    if a.output_dir:
        out = a.output_dir.resolve()
        out.mkdir(mode=0o700, parents=False, exist_ok=False)
    else:
        out = Path(tempfile.mkdtemp(prefix="pt516-boot-vbios-", dir="/var/tmp"))
    metadata = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_decode" if a.decode else "retained_snapshot",
        "source": str(a.decode or NODE),
        "complete_read": False,
    }
    rc = 1
    try:
        if a.decode is None:
            metadata["kernel"] = os.uname().release
            metadata["driver_version"] = (
                Path("/sys/module/nvidia/version").read_text().strip()
            )
            metadata["driver_srcversion"] = (
                Path("/sys/module/nvidia/srcversion").read_text().strip()
            )
            if (
                metadata["kernel"] != "7.2.7"
                or metadata["driver_version"] != "595.99.02"
            ):
                raise ValueError("kernel/driver differs from the reviewed build")
            if metadata["driver_srcversion"] not in expected_srcversions():
                raise ValueError(
                    "loaded module is not one of the shipped builds (see module/provenance.json)"
                )
        blob = read_bounded(a.decode or NODE)
        (out / "snapshot.bin").write_bytes(blob)
        metadata["snapshot_bytes_read"] = len(blob)
        metadata.update(decode(blob))
        metadata["complete_read"] = True
        if metadata["state"] == "captured":
            (out / "flash-copy.bin").write_bytes(blob[HEADER_SIZE:])
            rc = 0
        else:
            metadata["interpretation"] = (
                "No image retained. This is not proof that no firmware image exists; "
                "the branch may be unreached or the recorded first attempt failed."
            )
            rc = 2
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
    (out / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))
    print(f"Private artifacts: {out}")
    print(
        "Return this output. No retry, live ROM access, Set or benchmark follows automatically."
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
