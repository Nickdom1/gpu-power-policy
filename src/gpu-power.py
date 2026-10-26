#!/usr/bin/env python3
"""Runtime GPU power policy; requires root and the patched NVIDIA 615.71.09 module."""
import argparse
import fcntl
import hashlib
import struct
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy
from rm import RM, VERSION

CONFIG = Path("/etc/gpu-power-policy.json")
RUN = Path("/run/gpu-power-policy")
PROC_GPUS = Path("/proc/driver/nvidia/gpus")
PCI_DEVICES = Path("/sys/bus/pci/devices")


def parser():
    p = argparse.ArgumentParser(description=__doc__, epilog=(
        "Target changes restart nvidia-powerd; target off removes the override. Current changes last "
        "until current off (rated), GPU reinitialization or reboot; off releases both. Current SET "
        "requires a verified live 615 layout; dump and status are read-only. Boot values come from NixOS."))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("rom", help="export retained boot VBIOS (never overwrite)").add_argument("output", type=Path)
    sub.add_parser("status", help="raw policy tables, image selection and target state")
    sub.add_parser("dump", help="capture raw A618/A61A/A619 replies").add_argument("directory", type=Path)
    sub.add_parser("off", help="release both runtime overrides: rated current, then stock powerd target")
    for command, unit in (("target", "W"), ("current", "mA")):
        sub.add_parser(command).add_argument("value", help=f"integer {unit}, or off")
    return p


def bounded(value, lower, upper):
    if not re.fullmatch(r"[0-9]+", value) or not lower <= int(value) <= upper:
        raise ValueError(f"value must be an integer within {lower}..{upper}")
    return int(value)


def command_output(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def guard(config):
    if os.geteuid() != 0:
        raise ValueError("run gpu-power as root")
    if Path("/sys/module/nvidia/version").read_text().strip() != VERSION:
        raise ValueError("loaded NVIDIA module must be " + VERSION)
    # Resolve against the booted generation, not the newly switched userspace.
    modinfo = [config["modinfo"], "-b", "/run/booted-system/kernel-modules", "-k", os.uname().release]
    if (command_output(*modinfo, "-F", "version", "nvidia") != VERSION or
            command_output(*modinfo, "-F", "vbios_override_per_gpu", "nvidia") != "1"):
        raise ValueError("booted NVIDIA module lacks vbios_override_per_gpu")


def selection(log, bdf):
    lines = [s for s in log.splitlines() if f"vbios_override: GPU {bdf.rsplit('.', 1)[0]} " in s]
    if not lines:
        return {"state": "unknown", "line": "no override selection record in this boot"}
    line = lines[-1]
    state = "rejected" if "rejected" in line or "using stock VBIOS" in line else (
        "selected" if " selected:" in line else "unknown")
    return {"state": state, "line": line, "meaning": "host staging; verify firmware limits separately"}


def image_selection(config):
    try:
        log = command_output(config["journalctl"], "-k", "-b", "--no-pager", "-o", "cat")
    except subprocess.CalledProcessError:
        log = command_output(config["dmesg"])
    return selection(log, config["device"])


def target(config, value):
    path = RUN / "target-mw"
    if value == "off":
        path.unlink(missing_ok=True)
    else:
        watts = bounded(value, 1, config["powerCeilingWatts"])
        pending = RUN / "target-mw.new"
        pending.write_text(str(watts * 1000) + "\n")
        pending.replace(path)
    subprocess.run([config["systemctl"], "restart", "nvidia-powerd.service"], check=True)
    return {"target_mw": int(path.read_text()) if path.exists() else None,
            "powerd": command_output(config["systemctl"], "is-active", "nvidia-powerd.service"),
            "power_readback": command_output(config["nvidiaSmi"], "-i", config["device"], "-q", "-d", "POWER")}


def export_rom(config, output):
    bdf = config["device"]
    with (PROC_GPUS / bdf / "boot_vbios").open("rb") as source:
        blob = source.read(64 + 1024 * 1024 + 1)
    if not 64 <= len(blob) <= 64 + 1024 * 1024 or blob[:8] != b"NVBROM1\0":
        raise ValueError("invalid boot VBIOS snapshot size or magic")
    (version, header, state, rm, size, offset, domain, bus, slot, func,
     vendor, device, r0, r1) = struct.unpack_from("<14I", blob, 8)
    if version != 1 or header != 64 or r0 or r1:
        raise ValueError("unsupported or malformed boot VBIOS header")
    if f"{domain:04x}:{bus:02x}:{slot:02x}.{func:x}" != bdf or any(
            value != int((PCI_DEVICES / bdf / name).read_text(), 16)
            for name, value in (("vendor", vendor), ("device", device))):
        raise ValueError("boot VBIOS PCI identity does not match configured device")
    if state != 1:
        reason = {0: "extraction not reached", 2: "extraction failed", 3: "invalid image",
                  4: "retention allocation failed"}.get(state, f"unknown state {state}")
        raise ValueError(f"boot VBIOS unavailable: {reason} (RM status {rm:#x})")
    if rm or not 0 < size <= 1024 * 1024 or len(blob) != 64 + size or offset > size:
        raise ValueError("captured boot VBIOS has invalid status, size or expansion offset")
    image = blob[64:]
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
        os.fchmod(dest.fileno(), 0o600)
        dest.write(image)
    version = re.search(rb"[0-9A-Fa-f]{2}(?:\.[0-9A-Fa-f]{2}){4}", image)
    return {"size": size, "sha256": hashlib.sha256(image).hexdigest(),
            "expansion_rom_offset": offset, "vbios_version": version[0].decode() if version else None}


def run(args, config):
    if args.command == "rom":
        return export_rom(config, args.output)
    if args.command in ("target", "current", "off") and "output_sha256" not in config:
        raise ValueError("bootstrap mode has no override image; configure vbios and rebuild first")
    if args.command == "off":
        result = {}
        # Current can only have been raised on a staged image; otherwise there is nothing to restore.
        if image_selection(config)["state"] == "selected":
            with RM(config["device"]) as rm:
                rated = config["currentRatedMilliamps"]
                result["current_ma"] = policy.set_current(rm, policy.query(rm, policy.INFO), rated,
                                                          config["currentCeilingMilliamps"], rated)
        result.update(target(config, "off"))
        return result
    if args.command == "target":
        if args.value != "off" and image_selection(config)["state"] != "selected":
            raise ValueError("override image is not selected; refusing target override")
        return target(config, args.value)
    with RM(config["device"]) as rm:
        info = policy.query(rm, policy.INFO)
        if args.command == "current":
            if image_selection(config)["state"] != "selected":
                raise ValueError("override image is not selected; refusing current SET")
            rated, ceiling = config["currentRatedMilliamps"], config["currentCeilingMilliamps"]
            value = rated if args.value == "off" else bounded(args.value, rated, ceiling)
            return {"current_ma": policy.set_current(rm, info, rated, ceiling, value)}
        control = policy.query(rm, policy.GET, policy.u32(info, 4))
        if args.command == "dump":
            status = policy.query(rm, policy.STATUS, policy.u32(info, 4))
            args.directory.mkdir(parents=True, exist_ok=False)
            for name, raw in (("a618", info), ("a61a", control), ("a619", status)):
                (args.directory / (name + ".bin")).write_bytes(raw)
            (args.directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
            return {"dump": str(args.directory), "version": VERSION, "device": config["device"]}
        path = RUN / "target-mw"
        result = {"version": VERSION, "device": config["device"], "image": image_selection(config),
                  "power_readback": command_output(config["nvidiaSmi"], "-i", config["device"], "-q", "-d", "POWER"),
                  "target_mw": int(path.read_text()) if path.exists() else None,
                  "current_layout_verified": policy.CURRENT_LAYOUT_VERIFIED,
                  "a61a_header": control[:20].hex(), "a61a_entries": policy.table(policy.GET, control),
                  "a618_header": info[:0xCC].hex(), "a618_entries": policy.table(policy.INFO, info),
                  "configured": {k: v for k, v in config.items() if k.endswith(("Watts", "Milliamps"))}}
        if "output_sha256" not in config:
            return result
        try:
            index, current, low, high = policy.current_policy(info, control,
                config["currentRatedMilliamps"], config["currentCeilingMilliamps"])
            result["current_candidate"] = dict(index=index, milliamps=current, lower=low, upper=high)
        except ValueError as exc:
            result["current_decode_error"] = str(exc)
        return result


def main():
    args = parser().parse_args()
    try:
        config = json.loads(CONFIG.read_text())
        guard(config)
        RUN.mkdir(mode=0o755, exist_ok=True)
        with (RUN / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(json.dumps(run(args, config), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f"gpu-power: {exc}")


if __name__ == "__main__":
    main()
