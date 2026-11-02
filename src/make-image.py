#!/usr/bin/env python3
"""Edit a VBIOS ROM with nvidia-vbios-vix-editor and export the loader payload.

usage: make-image.py ROM REQUEST.json OUTDIR

The editor discovers the power-policy and NVVDD current rows structurally; an
image it does not recognise, or a value outside its bounds, is a hard error.
The payload must stay within the 512 KiB the Ampere/Ada GSP firmware reads, so
those families are exported with the editor's relocated representation.
"""

import json
import os
import re
import sys
from pathlib import Path

import baseline

GSP_IMAGE_LIMIT = 512 * 1024
REPRESENTATION = {"ampere": "ampere-relocated-v1", "ada": "ada-relocated-v2", "turing": None}


def nvbios(*args):
    return baseline.editor("nvbios", *args)


def main(rom, request_path, output):
    request = json.loads(Path(request_path).read_text())
    allowed = set(baseline.CEILINGS) | {
        "device", "vbios", "beyondTested", "powerdTargetWatts", "dynamicBoostEnabled",
    }
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    if request.keys() - allowed:
        raise ValueError("unknown requested field: " + ", ".join(sorted(request.keys() - allowed)))
    bdf = request.get("device", "")
    if not isinstance(bdf, str) or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", bdf):
        raise ValueError("device must be a PCI BDF")
    rom_info = baseline.read_rom(rom)
    values = baseline.propose(rom_info, request)
    target = request.get("powerdTargetWatts")
    if target is not None and target != "max" and (
        type(target) is not int or not 0 < target <= values["powerCeilingWatts"]
    ):
        raise ValueError("boot power target must be max or watts within the proposed ceiling")
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    edited, payload = destination / "edited.rom", destination / "payload.bin"
    # Only editable fields are passed to the editor; read-only seeds stay untouched.
    edits = {field: values[key] for key, field in baseline.FIELDS.items() if rom_info["editable"][key]}
    effective = Path(edited).with_suffix(".changes.json")
    effective.write_text(json.dumps(edits))
    report = nvbios("edit", rom, edited, "--changes", str(effective))
    applied = {f["field"]: f["after"] for f in report["affected_fields"]}
    after = baseline.read_rom(edited)
    if after["values"] != values or after["pci_device"] != rom_info["pci_device"]:
        raise ValueError("editor output differs from the requested ceilings or preserved ROM boot seeds")
    family = rom_info["family"]
    args = ["normalize", str(edited), str(payload)]
    if REPRESENTATION[family]:
        args += ["--representation", REPRESENTATION[family]]
    result = nvbios(*args)
    if os.path.getsize(payload) > GSP_IMAGE_LIMIT:
        sys.exit(f"payload is {os.path.getsize(payload)} bytes, above the {GSP_IMAGE_LIMIT}-byte GSP limit")
    metadata = json.loads(Path(str(payload) + ".json").read_text())
    runtime = {**request, **values, "romBaseline": rom_info}
    runtime.update({key: metadata[key] for key in ("output_size", "output_sha256")})
    (destination / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
    (destination / "modprobe.conf").write_text(
        f'options nvidia NVreg_RegistryDwordsPerDevice="pci={bdf};RmLinuxVbiosOverride=2;'
        f'RmLinuxVbiosOverrideSize={runtime["output_size"]}" NVreg_RegistryBinaryFilePerDevice="pci={bdf};'
        f'RmLinuxVbiosOverrideImage=vbios-override/{bdf}.bin"\n'
    )
    print(
        f"family {family}, edited {json.dumps(applied)}, "
        f"payload {os.path.getsize(payload)} bytes sha256 {result['sha256']}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    try:
        main(*sys.argv[1:])
    except (OSError, ValueError) as exc:
        sys.exit(f"make-image: {exc}")
