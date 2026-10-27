#!/usr/bin/env python3
"""Edit a VBIOS ROM with nvidia-vbios-vix-editor and export the loader payload.

usage: make-image.py ROM CHANGES.json EDITED.rom PAYLOAD.bin

The editor discovers the power-policy and NVVDD current rows structurally; an
image it does not recognise, or a value outside its bounds, is a hard error.
The payload must stay within the 512 KiB the Ampere/Ada GSP firmware reads, so
those families are exported with the editor's relocated representation.
"""

import json
import os
import subprocess
import sys

GSP_IMAGE_LIMIT = 512 * 1024
REPRESENTATION = {"ampere": "ampere-relocated-v1", "ada": "ada-relocated-v2", "turing": None}


def nvbios(*args):
    try:
        result = subprocess.run(["nvbios", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("nvbios command not found; install the pinned nvidia-vbios-vix-editor first")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        sys.exit("nvbios produced no JSON: " + result.stderr.strip())
    if not report.get("ok"):
        sys.exit(f"nvbios {args[0]} failed: {json.dumps(report.get('error'), indent=1)}")
    return report["result"]


def main(rom, changes_path, edited, payload):
    with open(changes_path) as source:
        changes = json.load(source)
    family = nvbios("inspect", rom)["architecture"]["family"]
    if family not in REPRESENTATION:
        sys.exit(f"unsupported VBIOS family {family!r}; only Turing, Ampere and Ada are handled")
    report = nvbios("edit", rom, edited, "--changes", changes_path)
    applied = {f["field"]: f["after"] for f in report["affected_fields"]}
    after_fields = {f["key"]: f["current"] for f in report["after"]["fields"]}
    for key, value in changes.items():
        # A requested value equal to the stored one is not listed as affected.
        if key not in applied and key not in after_fields:
            sys.exit(f"nvbios edit returned no value for requested field {key!r}")
        after = applied[key] if key in applied else after_fields[key]
        if after != value:
            sys.exit(f"field {key} is {after!r} after the edit, expected {value!r}")
    args = ["normalize", edited, payload]
    if REPRESENTATION[family]:
        args += ["--representation", REPRESENTATION[family]]
    result = nvbios(*args)
    if os.path.getsize(payload) > GSP_IMAGE_LIMIT:
        sys.exit(f"payload is {os.path.getsize(payload)} bytes, above the {GSP_IMAGE_LIMIT}-byte GSP limit")
    print(f"family {family}, edited {json.dumps(applied)}, "
          f"payload {os.path.getsize(payload)} bytes sha256 {result['sha256']}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
