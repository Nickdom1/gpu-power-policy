#!/usr/bin/env python3
"""Runtime GPU power policy; requires root and the patched NVIDIA 615.71.09 module."""

import argparse
import ctypes
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
BOOTED_MODULES = Path("/run/booted-system/kernel-modules")
LOADED_VERSION = Path("/sys/module/nvidia/version")


def parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Target changes restart nvidia-powerd; target off removes the override. Current changes last "
            "until current off (rated), GPU reinitialization or reboot; off releases all overrides. Current SET "
            "requires a verified live 615 layout; dump and status are read-only. Boot values come from the "
            "install configuration."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("rom", help="export retained boot VBIOS (never overwrite)").add_argument("output", type=Path)
    sub.add_parser("status", help="raw policy tables, image selection and target state")
    sub.add_parser("dump", help="capture raw A618/A61A/A619 replies").add_argument("directory", type=Path)
    sub.add_parser("off", help="independently restore rated current, remove target and restore stock limit")
    sub.add_parser("boot-init", help="initialize the configured boot target after checking image selection")
    for command, unit in (("target", "W"), ("current", "mA"), ("limit", "W")):
        sub.add_parser(command).add_argument("value", help=f"integer {unit}, or off")
    return p


def bounded(value, lower, upper):
    if not re.fullmatch(r"[0-9]+", value) or not lower <= int(value) <= upper:
        raise ValueError(f"value must be an integer within {lower}..{upper}")
    return int(value)


def command_output(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("runtime config must be a JSON object")
    required = ["device", "modinfo", "systemctl", "journalctl", "dmesg", "nvidiaSmi"]
    limits = ["powerCeilingWatts", "powerDefaultWatts", "currentCeilingMilliamps", "currentRatedMilliamps"]
    configured = any(key in config for key in [*limits, "output_size", "output_sha256"])
    if configured:
        required += [*limits, "output_size", "output_sha256"]
    for key in required:
        if key not in config:
            raise ValueError(f"runtime config missing required key: {key}")
    for key in ["device", "modinfo", "systemctl", "journalctl", "dmesg", "nvidiaSmi"]:
        if not isinstance(config[key], str) or not config[key]:
            raise ValueError(f"runtime config {key} must be a nonempty string")
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", config["device"]):
        raise ValueError("runtime config device must be a PCI BDF")
    if configured:
        for key in limits:
            if type(config[key]) is not int or not 1 <= config[key] <= 4294967:
                raise ValueError(f"runtime config {key} must be a positive integer limit")
        if (config["powerDefaultWatts"] > config["powerCeilingWatts"]
                or config["currentRatedMilliamps"] > config["currentCeilingMilliamps"]):
            raise ValueError("runtime config defaults must not exceed ceilings")
        if type(config["output_size"]) is not int or not 0 < config["output_size"] <= 512 * 1024:
            raise ValueError("runtime config output_size is invalid")
        if not isinstance(config["output_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", config["output_sha256"]):
            raise ValueError("runtime config output_sha256 is invalid")
    target_watts = config.get("powerdTargetWatts")
    if target_watts is not None and (
        not configured or type(target_watts) is not int or not 1 <= target_watts <= config["powerCeilingWatts"]
    ):
        raise ValueError("runtime config powerdTargetWatts requires an image and must be within its ceiling")
    return config


def guard(config):
    if os.geteuid() != 0:
        raise ValueError("run gpu-power as root")
    if LOADED_VERSION.read_text().strip() != VERSION:
        raise ValueError("loaded NVIDIA module must be " + VERSION)
    # NixOS retains the booted generation; other systems use the running kernel's tree.
    modinfo = [config["modinfo"]]
    if BOOTED_MODULES.is_dir():
        modinfo += ["-b", str(BOOTED_MODULES)]
    modinfo += ["-k", os.uname().release]
    if (
        command_output(*modinfo, "-F", "version", "nvidia") != VERSION
        or command_output(*modinfo, "-F", "vbios_override_per_gpu", "nvidia") != "1"
    ):
        raise ValueError("booted NVIDIA module lacks vbios_override_per_gpu")
    if not BOOTED_MODULES.is_dir() and not (PROC_GPUS / config["device"] / "boot_vbios").is_file():
        raise ValueError("loaded NVIDIA module lacks boot VBIOS export; reboot into the patched module")


def selection(log, bdf):
    # The pinned driver logs domain:bus:slot without a function. Do not attribute
    # that legacy record to a nonzero function; a full BDF record is unambiguous.
    fields = [bdf]
    if bdf.endswith(".0"):
        fields.append(bdf[:-2])
    lines = [s for s in log.splitlines() if any(f"vbios_override: GPU {field} " in s for field in fields)]
    if not lines:
        return {"state": "unknown", "line": "no override selection record in this boot"}
    line = lines[-1]
    state = (
        "rejected"
        if "rejected" in line or "using stock VBIOS" in line
        else ("selected" if " selected:" in line else "unknown")
    )
    return {"state": state, "line": line, "meaning": "host staging; verify firmware limits separately"}


def image_selection(config):
    try:
        log = command_output(config["journalctl"], "-k", "-b", "--no-pager", "-o", "cat")
    except subprocess.CalledProcessError:
        log = command_output(config["dmesg"])
    return selection(log, config["device"])


def service_state(config, action):
    result = subprocess.run(
        [config["systemctl"], action, "nvidia-powerd.service"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "inactive"


def require_image(config):
    if "output_sha256" not in config:
        raise ValueError("bootstrap mode has no override image; configure vbios and reinstall or rebuild first")
    if image_selection(config)["state"] != "selected":
        raise ValueError("override image is not selected; refusing policy change")


def write_target(path, data):
    pending = path.with_suffix(".new")
    pending.write_bytes(data)
    pending.replace(path)


def target(config, value):
    path = RUN / "target-mw"
    # Release the file even when the daemon is absent; a numeric target must be a no-op then.
    if value == "off":
        path.unlink(missing_ok=True)
    if service_state(config, "is-active") != "active":
        return {
            "powerd": "inactive",
            "changed": value == "off",
            "message": "nvidia-powerd is not running; " + ("target removed" if value == "off" else "target unchanged"),
        }
    previous = path.read_bytes() if path.exists() else None
    if value != "off":
        require_image(config)
        watts = bounded(value, 1, config["powerCeilingWatts"])
        write_target(path, f"{watts * 1000}\n".encode())
    try:
        subprocess.run(
            [config["systemctl"], "restart", "nvidia-powerd.service"], check=True, capture_output=True, text=True
        )
        if service_state(config, "is-active") != "active":
            raise RuntimeError("nvidia-powerd exited after restart")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        if value != "off":
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_target(path, previous)
        state = "previous target restored" if value != "off" else "target remains removed"
        raise RuntimeError(f"powerd restart failed; {state}: {exc}") from exc
    return {
        "target_mw": int(path.read_text()) if path.exists() else None,
        "powerd": "active",
        "power_readback": command_output(config["nvidiaSmi"], "-i", config["device"], "-q", "-d", "POWER"),
    }


def power_readback(config):
    enforced = command_output(
        config["nvidiaSmi"], "-i", config["device"], "--query-gpu=enforced.power.limit", "--format=csv,noheader,nounits"
    )
    return {"enforced_watts": float(enforced)}


def standard_limit(config, watts):
    # nvidia-smi treats NVML_ERROR_NOT_SUPPORTED as a warning and exits zero.
    # Use NVML's actual status rather than interpreting the command's exit code.
    library = "/run/opengl-driver/lib/libnvidia-ml.so.1"
    nvml = ctypes.CDLL(library if Path(library).exists() else "libnvidia-ml.so.1")
    nvml.nvmlInit_v2.argtypes, nvml.nvmlInit_v2.restype = [], ctypes.c_uint
    nvml.nvmlShutdown.argtypes, nvml.nvmlShutdown.restype = [], ctypes.c_uint
    nvml.nvmlErrorString.argtypes, nvml.nvmlErrorString.restype = [ctypes.c_uint], ctypes.c_char_p
    nvml.nvmlDeviceGetHandleByPciBusId_v2.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    nvml.nvmlDeviceGetHandleByPciBusId_v2.restype = ctypes.c_uint
    nvml.nvmlDeviceSetPowerManagementLimit.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    nvml.nvmlDeviceSetPowerManagementLimit.restype = ctypes.c_uint

    def check(status):
        if status:
            raise RuntimeError("NVML setup: " + nvml.nvmlErrorString(status).decode())

    check(nvml.nvmlInit_v2())
    try:
        device = ctypes.c_void_p()
        check(nvml.nvmlDeviceGetHandleByPciBusId_v2(config["device"].encode(), ctypes.byref(device)))
        status = nvml.nvmlDeviceSetPowerManagementLimit(device, watts * 1000)
        return {"status": status, "message": nvml.nvmlErrorString(status).decode()}
    finally:
        nvml.nvmlShutdown()


def limit(config, value):
    require_image(config)
    watts = bounded(str(config["powerDefaultWatts"]) if value == "off" else value, 1, config["powerCeilingWatts"])
    before = power_readback(config)
    standard = standard_limit(config, watts)
    path = "standard"
    if standard["status"]:
        with RM(config["device"]) as rm:
            rm.control(policy.SET_LIMIT, policy.limit_request(watts))
        path = "role-8"
    after = power_readback(config)
    moved = before["enforced_watts"] != after["enforced_watts"]
    return {
        "path": path,
        "requested_watts": watts,
        "before": before,
        "after": after,
        "changed": moved,
        "message": "enforced limit changed" if moved else "enforced limit did not move",
        "standard_status": standard["status"],
        "standard_output": standard["message"],
    }


def current(config, value):
    if value != "off":
        require_image(config)
    if "currentRatedMilliamps" not in config:
        raise ValueError("bootstrap mode has no configured rated current; configure vbios first")
    with RM(config["device"]) as rm:
        rated, ceiling = config["currentRatedMilliamps"], config["currentCeilingMilliamps"]
        info = policy.query(rm, policy.INFO)
        if value == "off":
            control = policy.query(rm, policy.GET, policy.u32(info, 4))
            _, live, _, _ = policy.current_policy(info, control, rated, ceiling, restoring=True)
            if live <= rated:
                return {"current_ma": live}
        milliamps = rated if value == "off" else bounded(value, rated, ceiling)
        return {"current_ma": policy.set_current(rm, info, rated, ceiling, milliamps)}


def release(config):
    result = {"ok": True, "steps": {}}
    for name, action in (("current", current), ("target", target), ("limit", limit)):
        try:
            result["steps"][name] = {"ok": True, "result": action(config, "off")}
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            result["ok"] = False
            result["steps"][name] = {"ok": False, "error": str(exc)}
    return result


def dynamic_boost(config):
    try:
        text = (PROC_GPUS / config["device"] / "power").read_text()
    except OSError:
        return False
    return bool(re.search(r"^Notebook Dynamic Boost:\s+Supported\s*$", text, re.MULTILINE))


def boot_init(config):
    if image_selection(config)["state"] != "selected":
        return {"boot": "skipped", "message": "override image not selected; keeping stock behavior"}
    watts = config.get("powerdTargetWatts")
    path = RUN / "target-mw"
    if watts is None:
        path.unlink(missing_ok=True)
        return {"boot": "stock", "target_mw": None}
    watts = bounded(str(watts), 1, config["powerCeilingWatts"])
    if dynamic_boost(config) and service_state(config, "is-enabled") in ("enabled", "enabled-runtime"):
        write_target(path, f"{watts * 1000}\n".encode())
        return {"boot": "target", "target_mw": watts * 1000}
    path.unlink(missing_ok=True)
    return {"boot": "limit", **limit(config, str(watts))}


def export_rom(config, output):
    bdf = config["device"]
    with (PROC_GPUS / bdf / "boot_vbios").open("rb") as source:
        blob = source.read(64 + 1024 * 1024 + 1)
    if not 64 <= len(blob) <= 64 + 1024 * 1024 or blob[:8] != b"NVBROM1\0":
        raise ValueError("invalid boot VBIOS snapshot size or magic")
    (version, header, state, rm, size, offset, domain, bus, slot, func, vendor, device, r0, r1) = struct.unpack_from(
        "<14I", blob, 8
    )
    if version != 1 or header != 64 or r0 or r1:
        raise ValueError("unsupported or malformed boot VBIOS header")
    if f"{domain:04x}:{bus:02x}:{slot:02x}.{func:x}" != bdf or any(
        value != int((PCI_DEVICES / bdf / name).read_text(), 16)
        for name, value in (("vendor", vendor), ("device", device))
    ):
        raise ValueError("boot VBIOS PCI identity does not match configured device")
    if state != 1:
        reason = {
            0: "extraction not reached",
            2: "extraction failed",
            3: "invalid image",
            4: "retention allocation failed",
        }.get(state, f"unknown state {state}")
        raise ValueError(f"boot VBIOS unavailable: {reason} (RM status {rm:#x})")
    if rm or not 0 < size <= 1024 * 1024 or len(blob) != 64 + size or offset > size:
        raise ValueError("captured boot VBIOS has invalid status, size or expansion offset")
    image = blob[64:]
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
        os.fchmod(dest.fileno(), 0o600)
        dest.write(image)
    version = re.search(rb"[0-9A-Fa-f]{2}(?:\.[0-9A-Fa-f]{2}){4}", image)
    return {
        "size": size,
        "sha256": hashlib.sha256(image).hexdigest(),
        "expansion_rom_offset": offset,
        "vbios_version": version[0].decode() if version else None,
    }


def run(args, config):
    if args.command == "rom":
        return export_rom(config, args.output)
    if args.command == "off":
        return release(config)
    if args.command == "boot-init":
        return boot_init(config)
    if args.command == "target":
        return target(config, args.value)
    if args.command == "limit":
        return limit(config, args.value)
    if args.command == "current":
        return current(config, args.value)
    with RM(config["device"]) as rm:
        info = policy.query(rm, policy.INFO)
        control = policy.query(rm, policy.GET, policy.u32(info, 4))
        if args.command == "dump":
            status = policy.query(rm, policy.STATUS, policy.u32(info, 4))
            args.directory.mkdir(parents=True, exist_ok=False)
            for name, raw in (("a618", info), ("a61a", control), ("a619", status)):
                (args.directory / (name + ".bin")).write_bytes(raw)
            (args.directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
            return {"dump": str(args.directory), "version": VERSION, "device": config["device"]}
        path = RUN / "target-mw"
        result = {
            "version": VERSION,
            "device": config["device"],
            "image": image_selection(config),
            "power_readback": command_output(config["nvidiaSmi"], "-i", config["device"], "-q", "-d", "POWER"),
            "target_mw": int(path.read_text()) if path.exists() else None,
            "powerd": service_state(config, "is-active"),
            "dynamic_boost": dynamic_boost(config),
            "a61a_header": control[:20].hex(),
            "a61a_entries": policy.table(policy.GET, control),
            "a618_header": info[:0xCC].hex(),
            "a618_entries": policy.table(policy.INFO, info),
            "configured": {k: v for k, v in config.items() if k.endswith(("Watts", "Milliamps"))},
        }
        if "output_sha256" not in config:
            return result
        try:
            index, current_ma, low, high = policy.current_policy(
                info, control, config["currentRatedMilliamps"], config["currentCeilingMilliamps"]
            )
            result["current_candidate"] = dict(index=index, milliamps=current_ma, lower=low, upper=high)
        except ValueError as exc:
            result["current_decode_error"] = str(exc)
        return result


def main():
    args = parser().parse_args()
    try:
        config = validate_config(json.loads(CONFIG.read_text()))
        if args.command == "boot-init":
            command_output(str(Path(config["modinfo"]).with_name("modprobe")), "nvidia")
            command_output(config["nvidiaSmi"], "-i", config["device"], "-q", "-d", "POWER")
        if args.command != "off":
            guard(config)
        RUN.mkdir(mode=0o755, exist_ok=True)
        with (RUN / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = run(args, config)
            print(json.dumps(result, indent=None if args.command == "boot-init" else 2))
            if result.get("ok") is False:
                sys.exit(1)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f"gpu-power: {exc}")


if __name__ == "__main__":
    main()
