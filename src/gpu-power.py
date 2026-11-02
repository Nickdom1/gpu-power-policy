#!/usr/bin/env python3
"""Runtime GPU power policy; requires root and the patched NVIDIA 615.78.08 module."""

import argparse
import ctypes
import fcntl
import hashlib
import struct
import json
import os
import platform
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy
import baseline
from rm import RM, VERSION

CONFIG = Path("/etc/gpu-power-policy.json")
RUN = Path("/run/gpu-power-policy")
PROC_GPUS = Path("/proc/driver/nvidia/gpus")
PCI_DEVICES = Path("/sys/bus/pci/devices")
ROM_STORE = Path("/var/lib/gpu-power-policy/roms")
BOOTED_MODULES = Path("/run/booted-system/kernel-modules")
LOADED_VERSION = Path("/sys/module/nvidia/version")
REPAIRS = {
    "linux": {
        "reinstall": "sudo bash install/linux.sh, then reboot",
        "recapture": "sudo bash install/linux.sh, then reboot",
        "disable": "sudo bash install/linux.sh --disable, then reboot",
        "preflight": "bash install/preflight.sh",
        "blacklist": "sudo bash install/linux.sh --disable, then reboot",
    },
    "nixos": {
        "reinstall": "rebuild from your configuration and reboot",
        "recapture": (
            "rerun sudo gpu-power configure --nix with your chosen ceiling flags, replace the "
            "saved module with its output, git add it, then rebuild and reboot"
        ),
        "disable": "set hardware.gpuPowerPolicy.enable = false, rebuild and reboot, or boot the previous generation",
        "preflight": "nix run github:Nickdom1/gpu-power-policy#preflight",
        "blacklist": (
            "set hardware.gpuPowerPolicy.enable = false and rebuild, or select the previous generation; "
            "reboot"
        ),
    },
}
try:
    PLATFORM = "nixos" if platform.freedesktop_os_release().get("ID") == "nixos" else "linux"
except OSError:
    PLATFORM = "linux"

REPAIR = REPAIRS[PLATFORM]


def repair_commands(config, platform_name=PLATFORM):
    repairs = REPAIRS[platform_name].copy()
    checkout = config.get("installerCheckout") if isinstance(config, dict) else None
    checkout = Path(checkout) if isinstance(checkout, str) else Path(".")
    if platform_name == "linux" and checkout.is_absolute() and (checkout / "install/linux.sh").is_file():
        for name, command in repairs.items():
            for script in ("linux.sh", "preflight.sh"):
                command = command.replace("install/" + script, shlex.quote(str(checkout / "install" / script)))
            repairs[name] = command
    return repairs


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device")
    p.add_argument("--json", action="store_true", help="machine-readable result")
    sub = p.add_subparsers(dest="command", required=True, metavar="{rom,configure,status,dump,off,power,current}")
    sub.add_parser("rom", help="export retained boot VBIOS").add_argument("output", type=Path)
    configure = sub.add_parser("configure", help="derive ceilings from the captured ROM; apply nothing")
    configure.add_argument("--nix", action="store_true")
    configure.add_argument("--power-ceiling")
    configure.add_argument("--current-ceiling")
    configure.add_argument("--beyond-tested", action="store_true")
    configure.add_argument("--boot-power")
    sub.add_parser("status", help="readiness and next command").add_argument("--full", action="store_true")
    sub.add_parser("dump", help="capture raw policy replies").add_argument("directory", type=Path)
    sub.add_parser("off", help="release this boot's overrides")
    sub.add_parser("boot-init")
    for command in ("power", "current"):
        item = sub.add_parser(command, help=command)
        item.add_argument("value", help="max, +N%%, integer W/mA, or off")
    for item in sub.choices.values():
        item.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return p


def command_output(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def nvidia_blacklisted(cmdline=None):
    if cmdline is None:
        cmdline = Path("/proc/cmdline").read_text()
    for token in cmdline.split():
        key, _, modules = token.partition("=")
        if key in ("modprobe.blacklist", "rd.driver.blacklist", "module_blacklist"):
            if any(name.replace("-", "_") == "nvidia" for name in modules.split(",")):
                return True
    return False


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("runtime config must be a JSON object")
    required = ["device"]
    limits = ["powerCeilingWatts", "powerDefaultWatts", "currentCeilingMilliamps", "currentRatedMilliamps"]
    configured = any(key in config for key in [*limits, "output_size", "output_sha256"])
    if configured:
        required += [*limits, "output_size", "output_sha256"]
    for key in required:
        if key not in config:
            raise ValueError(f"runtime config missing required key: {key}")
    if config["device"] is None and not configured:
        gpus = [
            p.name
            for p in PCI_DEVICES.iterdir()
            if (p / "vendor").read_text().strip() == "0x10de" and int((p / "class").read_text(), 16) >> 16 == 3
        ]
        if len(gpus) != 1:
            raise ValueError("set device explicitly: expected exactly one NVIDIA display GPU, found " + str(len(gpus)))
        config["device"] = gpus[0]
    if not isinstance(config["device"], str) or not re.fullmatch(
        r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", config["device"]
    ):
        raise ValueError("runtime config device must be a PCI BDF")
    if configured:
        for key in limits:
            if type(config[key]) is not int or not 1 <= config[key] <= 4294967:
                raise ValueError(f"runtime config {key} must be a positive integer limit")
        if (
            config["powerDefaultWatts"] > config["powerCeilingWatts"]
            or config["currentRatedMilliamps"] > config["currentCeilingMilliamps"]
        ):
            raise ValueError("runtime config defaults must not exceed ceilings")
        if type(config["output_size"]) is not int or not 0 < config["output_size"] <= 512 * 1024:
            raise ValueError("runtime config output_size is invalid")
        if not isinstance(config["output_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", config["output_sha256"]):
            raise ValueError("runtime config output_sha256 is invalid")
    if "romBaseline" in config:
        rom = baseline.validate(config["romBaseline"])
        if any(config.get(key) != rom["values"][key] for key in baseline.SEEDS):
            raise ValueError("runtime boot defaults differ from ROM baseline; reinstall or rebuild")
    target_watts = config.get("powerdTargetWatts")
    if target_watts is not None and (
        target_watts != "max"
        and (
            type(target_watts) is not int
            or target_watts < 1
            or (configured and target_watts > config["powerCeilingWatts"])
        )
    ):
        raise ValueError("runtime config powerdTargetWatts must be max or watts within its ceiling")
    return config


def guard(config):
    if os.geteuid() != 0:
        raise ValueError("run gpu-power as root")
    if LOADED_VERSION.read_text().strip() != VERSION:
        raise ValueError("loaded NVIDIA module must be " + VERSION)
    # NixOS retains the booted generation; other systems use the running kernel's tree.
    modinfo = ["modinfo"]
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
        log = command_output("journalctl", "-k", "-b", "--no-pager", "-o", "cat")
    except subprocess.CalledProcessError:
        log = command_output("dmesg")
    return selection(log, config["device"])


def service_state(config, action):
    result = subprocess.run(
        ["systemctl", action, "nvidia-powerd.service"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "inactive"


def require_image(config, state=None):
    state = state or live_state(config)
    if state["verdict"] != "Ready":
        raise ValueError(state["message"])
    return state


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
        watts = runtime_value(config, "power", value)
        write_target(path, f"{watts * 1000}\n".encode())
    try:
        subprocess.run(
            ["systemctl", "restart", "nvidia-powerd.service"], check=True, capture_output=True, text=True
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
    readback = power_readback(config)
    message = "Power target removed" if value == "off" else f"Power target {watts} W set"
    return {
        "message": message + f"; nvidia-powerd restarted; enforced limit now {readback['enforced_watts']:g} W. "
        "Release: sudo gpu-power off",
        "target_mw": int(path.read_text()) if path.exists() else None,
        "powerd": "active",
        "power_readback": readback,
    }


def power_readback(config):
    enforced = command_output(
        "nvidia-smi", "-i", config["device"], "--query-gpu=enforced.power.limit", "--format=csv,noheader,nounits"
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
    state = live_state(config)
    if state["verdict"] == "Stale":
        raise ValueError(state["message"])
    watts = (
        int(state["rom"]["values"]["powerDefaultWatts"])
        if value == "off"
        else runtime_value(config, "power", value, state)
    )
    before = power_readback(config)
    standard = standard_limit(config, watts)
    path = "NVML"
    if standard["status"] == 3:
        with RM(config["device"]) as rm:
            rm.control(policy.SET_LIMIT, policy.limit_request(watts))
        path = "firmware"
    elif standard["status"]:
        raise RuntimeError(f"NVML refused power limit ({standard['status']}): {standard['message']}")
    marker = RUN / "limit-set"
    if value == "off":
        marker.unlink(missing_ok=True)
    else:
        write_target(marker, b"1\n")
    after = power_readback(config)
    return {
        "path": path,
        "requested_watts": watts,
        "before": before,
        "after": after,
        "changed": before != after,
        "standard_status": standard["status"],
        "message": f"Power limit {watts} W requested; enforced limit now {after['enforced_watts']:g} W. "
        "Release: sudo gpu-power off",
    }


def current(config, value):
    state = live_state(config)
    if state["verdict"] == "Stale" or not state.get("bounds"):
        raise ValueError(state["message"])
    rom = state["rom"]
    if rom["family"] == "ada":
        if value == "off":
            return {"changed": False, "message": "Ada current control is unavailable; nothing to release."}
        raise ValueError("Ada: current control is unavailable. Run: sudo gpu-power power max")
    if value != "off":
        require_image(config, state)
    rated = rom["values"]["currentRatedMilliamps"]
    index = rom["policies"]["current"]
    ceiling = min(
        state["bounds"]["current"][2], config.get("currentCeilingMilliamps", rom["values"]["currentCeilingMilliamps"])
    )
    if value == "off":
        ceiling = state["bounds"]["current"][2]
    with RM(config["device"]) as rm:
        info = policy.query(rm, policy.INFO)
        control = policy.query(rm, policy.GET, policy.u32(info, 4))
        _, actual, _, _ = policy.current_policy(info, control, rated, ceiling, index=index, restoring=value == "off")
        requested = rated if value == "off" else runtime_value(config, "current", value, state)
        if actual == requested or (value == "off" and actual <= rated):
            return {
                "current_ma": actual,
                "changed": False,
                "message": f"Current {actual / 1000:g} A is already set; "
                + ("this is the ROM rated value." if actual == rated else "nothing changed."),
            }
        actual = policy.set_current(rm, info, rated, ceiling, requested, index=index)
    return {
        "current_ma": actual,
        "changed": True,
        "message": f"Current {actual / 1000:g} A set; firmware readback confirmed. Release: sudo gpu-power off",
    }


def release(config):
    result = {"ok": True, "steps": {}}
    actions = [("current", current), ("target", target)]
    if (RUN / "limit-set").exists():
        actions.append(("limit", limit))
    for name, action in actions:
        try:
            result["steps"][name] = {"ok": True, "result": action(config, "off")}
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            result["ok"] = False
            result["steps"][name] = {"ok": False, "error": str(exc)}
    result["message"] = (
        "Overrides released."
        if result["ok"]
        else "Release incomplete: " + "; ".join(v["error"] for v in result["steps"].values() if not v["ok"])
    )
    return result


def dynamic_boost(config):
    try:
        text = (PROC_GPUS / config["device"] / "power").read_text()
    except OSError:
        return False
    return bool(re.search(r"^Notebook Dynamic Boost:\s+Supported\s*$", text, re.MULTILINE))


def boot_init(config):
    for name in ("target-mw", "ceiling-mw"):
        (RUN / name).unlink(missing_ok=True)
    state = live_state(config)
    write_target(RUN / "live.json", (json.dumps(state, indent=2) + "\n").encode())
    require_image(config, state)
    write_target(RUN / "device", (config["device"] + "\n").encode())
    write_target(RUN / "ceiling-mw", f"{state['bounds']['power'][2]}\n".encode())
    (RUN / "notice").unlink(missing_ok=True)
    watts = config.get("powerdTargetWatts")
    if watts is None:
        return {"boot": "stock", "target_mw": None, "message": state["message"]}
    watts = runtime_value(config, "power", str(watts), state)
    if uses_powerd(config):
        write_target(RUN / "target-mw", f"{watts * 1000}\n".encode())
        return {"boot": "target", "target_mw": watts * 1000}
    return {"boot": "limit", **limit(config, str(watts))}


def read_boot_rom(config):
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
    version = re.search(rb"[0-9A-Fa-f]{2}(?:\.[0-9A-Fa-f]{2}){4}", image)
    return image, {
        "size": size,
        "sha256": hashlib.sha256(image).hexdigest(),
        "pci_device": f"{vendor:04x}:{device:04x}",
        "expansion_rom_offset": offset,
        "vbios_version": version[0].decode() if version else None,
    }


def export_rom(config, output):
    image, metadata = read_boot_rom(config)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
        os.fchmod(dest.fileno(), 0o600)
        dest.write(image)
    return metadata


def captured_baseline(config):
    image, metadata = read_boot_rom(config)
    with tempfile.TemporaryDirectory(prefix="gpu-power-inspect-") as directory:
        rom = Path(directory) / "stock.rom"
        rom.write_bytes(image)
        result = baseline.read_rom(rom)
    result["version"] = metadata.get("vbios_version", result.get("version"))
    return result


def live_state(config, info=None):
    image, metadata = read_boot_rom(config)
    digest = hashlib.sha256(image).hexdigest()
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    old = config.get("romBaseline")
    if old and (old.get("sha256") != digest or metadata.get("pci_device", old["pci_device"]) != old["pci_device"]):
        swapped = metadata.get("pci_device", old["pci_device"]) != old["pci_device"]
        reason = (
            f"a different GPU ({metadata['pci_device']}) is present; image was built for {old['pci_device']}"
            if swapped
            else (
                f"your VBIOS changed ({old.get('version')} -> {metadata.get('vbios_version')}) "
                "since the image was built"
            )
        )
        fix = f"{REPAIR['disable']}, then {REPAIR['preflight']}" if swapped else REPAIR["recapture"]
        return {
            "device": config["device"],
            "boot_id": boot_id,
            "rom": None,
            "captured": metadata,
            "bounds": {},
            "baseline": {"match": False, "seeds_match": False},
            "image": image_selection(config),
            "verdict": "Stale",
            "mode": "raised",
            "message": f"Stale: {reason}; raising is refused. Run: {fix}.",
        }
    rom = None
    try:
        cached = json.loads((RUN / "live.json").read_text())
        if cached["device"] == config["device"] and cached["boot_id"] == boot_id and cached["rom"]["sha256"] == digest:
            rom = baseline.validate(cached["rom"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if rom is None:
        rom = captured_baseline(config)
    if info is None:
        with RM(config["device"]) as rm:
            info = policy.query(rm, policy.INFO)
    try:
        checked = baseline.compare(rom, config, info)
        rows = {r["index"]: r for r in policy.table(policy.INFO, info)}
        bounds = {name: rows[index]["words"][:3] for name, index in rom["policies"].items()}
    except ValueError as exc:
        checked = {"match": False, "seeds_match": False, "error": str(exc)}
        bounds = {}
    selection = image_selection(config)
    verdict, mode = "Ready", "raised" if "output_sha256" in config else "stock"
    fix = REPAIR["disable"]
    reason = ""
    if old and old["pci_device"] != rom["pci_device"]:
        verdict = "Stale"
        reason = f"a different GPU ({rom['pci_device']}) is present; image was built for {old['pci_device']}"
        fix += f", then {REPAIR['preflight']}"
    elif mode == "raised" and selection["state"] == "rejected":
        verdict, reason = "Unverified", selection["line"]
    elif (mode == "raised" and (old is None or selection["state"] != "selected")) or not checked["match"]:
        verdict, reason = "Unverified", "image/firmware maxima do not match the expected ceilings; raising is refused"
    elif not checked["seeds_match"]:
        verdict, reason = "Unverified", "firmware boot defaults differ from the captured ROM; raising is refused"
    if verdict != "Ready":
        message = f"{verdict}: {reason}. Run: {fix}."
        if verdict == "Unverified":
            message += " Boot escape: nvidia.NVreg_RegistryDwords=RmLinuxVbiosOverrideDisable=1"
    else:
        v = checked["live"]
        message = (
            f"Ready ({mode}): {VERSION} on {config['device']}, "
            + (
                "stock VBIOS. "
                if mode == "stock"
                else f"image from your ROM {rom.get('version') or 'unknown'}, firmware confirmed. "
            )
            + f"Power {v['powerDefaultWatts']:g} W default, {v['powerCeilingWatts']:g} W maximum. "
            + f"Current {v['currentRatedMilliamps'] / 1000:g} A rated, "
            + f"{v['currentCeilingMilliamps'] / 1000:g} A maximum."
        )
    return {
        "device": config["device"],
        "boot_id": boot_id,
        "rom": rom,
        "bounds": bounds,
        "baseline": checked,
        "image": selection,
        "verdict": verdict,
        "mode": mode,
        "message": message,
    }


def runtime_value(config, kind, value, state=None):
    state = require_image(config, state)
    key = "powerCeilingWatts" if kind == "power" else "currentCeilingMilliamps"
    scale = 1000 if kind == "power" else 1
    low, rated, high = state["bounds"][kind]
    upper = min(high / scale, config.get(key, state["rom"]["values"][key]))
    lower = low / scale if kind == "power" else rated
    number = (
        upper
        if value == "max"
        else baseline.amount(value, state["rom"]["values"][key], step=1000 if kind == "current" else 1)
    )
    if int(number) != number or not lower <= number <= upper:
        raise ValueError(
            f"{kind} must be within live firmware bounds {lower:g}..{upper:g}; Run: sudo gpu-power {kind} max"
        )
    return int(number)


def uses_powerd(config):
    return (
        dynamic_boost(config)
        and config.get("dynamicBoostEnabled", True)
        and service_state(config, "is-enabled") in ("enabled", "enabled-runtime")
    )


def power(config, value):
    return target(config, value) if uses_powerd(config) else limit(config, value)


def configure(args, config):
    image, exported = read_boot_rom(config)
    digest = hashlib.sha256(image).hexdigest()
    with tempfile.TemporaryDirectory(prefix="gpu-power-configure-") as directory:
        path = Path(directory) / "stock.rom"
        path.write_bytes(image)
        rom = baseline.read_rom(path)
        pci = PCI_DEVICES / config["device"]
        identity = ":".join(f"{int((pci / key).read_text(), 16):04x}" for key in ("vendor", "device"))
        if rom["pci_device"] != identity:
            raise ValueError(f"ROM PCI identity {rom['pci_device']} does not match selected GPU {identity}")
        with RM(config["device"]) as rm:
            checked = baseline.compare(rom, {}, policy.query(rm, policy.INFO))
        if not checked["seeds_match"]:
            baseline.require_match(checked)
        proposed = {"beyondTested": args.beyond_tested}
        for key, value in (
            ("powerCeilingWatts", args.power_ceiling),
            ("currentCeilingMilliamps", args.current_ceiling),
        ):
            if value is not None:
                proposed[key] = value
        values = baseline.propose(rom, proposed)
        boot = args.boot_power
        if boot is not None and boot != "max":
            boot = baseline.amount(boot, rom["values"]["powerCeilingWatts"])
            if not 0 < boot <= values["powerCeilingWatts"]:
                raise ValueError("--boot-power must be within the proposed power ceiling")
        raised = any(values[key] > rom["values"][key] for key in baseline.CEILINGS)
        rom_version = rom.get("version") or exported.get("vbios_version") or "unknown"
        print(f"ROM {rom_version}, sha256 {digest}.", file=sys.stderr)
        print("Value                         ROM stock    Live now    Proposed", file=sys.stderr)
        for key, label, scale in (
            ("powerCeilingWatts", "Power maximum (W)", 1),
            ("currentCeilingMilliamps", "Current maximum (A)", 1000),
        ):
            delta = (values[key] / rom["values"][key] - 1) * 100
            cap = "within +10% cap" if values[key] * 10 <= rom["values"][key] * 11 else "beyond +10% cap"
            print(
                f"{label:29} {rom['values'][key] / scale:>9g} {checked['live'][key] / scale:>11g} "
                f"{values[key] / scale:>11g}   +{delta:.1f}%, {cap}",
                file=sys.stderr,
            )
        print(
            "Boot defaults stay at the ROM's "
            f"{rom['values']['powerDefaultWatts']} W and {rom['values']['currentRatedMilliamps'] / 1000:g} A.",
            file=sys.stderr,
        )
        old = config.get("romBaseline")
        if old and old.get("sha256") != digest:
            print(
                f"The captured ROM ({rom_version}, {digest[:12]}) differs from the one this image was built from "
                f"({old.get('version') or 'unknown'}, {old.get('sha256', '')[:12]}). Rebuilding from the current ROM.",
                file=sys.stderr,
            )
        if raised:
            print(
                "Evidence for raising current: one RTX 3080 Ti Laptop (GA103), +8% current, external cooler, "
                "runs of two minutes or less; regulator temperature not measured. Run on AC, supervised.",
                file=sys.stderr,
            )
            if rom["family"] == "ada":
                print("Ada power raising is experimental; no current control is available.", file=sys.stderr)
            saved = ROM_STORE / (digest + ".rom")
            saved.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            saved.parent.parent.chmod(0o755)
            saved.parent.chmod(0o755)
            if saved.exists() and saved.read_bytes() != image:
                raise ValueError("saved ROM differs from the retained boot image; refusing overwrite")
            if not saved.exists():
                with os.fdopen(os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb") as dest:
                    dest.write(image)
            saved.chmod(0o644)
        else:
            saved = None
            print("Ceilings equal the ROM's; nothing to install. Stock-range controls already work.", file=sys.stderr)
    result = {"device": config["device"], "vbios": str(saved) if saved else None, "powerdTargetWatts": boot}
    if raised:
        result.update(
            {
                key: values[key]
                for key in baseline.CEILINGS
                if rom["family"] != "ada" or key != "currentCeilingMilliamps"
            }
        )
        result["beyondTested"] = args.beyond_tested
    if not args.nix:
        return json.dumps(result, indent=2)
    lines = ["hardware.gpuPowerPolicy = {", f'  device = "{config["device"]}";']
    if saved:
        url = json.dumps("file://" + quote(str(saved), safe="/"))
        lines.append(f'  vbios = builtins.fetchurl {{ url = {url}; sha256 = "{digest}"; }};')
        lines += [f"  {key} = {result[key]};" for key in baseline.CEILINGS if key in result]
    if args.beyond_tested:
        lines.append("  beyondTested = true;")
    if boot is not None:
        lines.append(f"  powerdTargetWatts = {json.dumps(boot)};")
    print("Save this module beside your host flake.nix, git add it, and add it to imports before rebuilding.",
          file=sys.stderr)
    return "{\n" + "\n".join("  " + line for line in [*lines, "};"]) + "\n}"


def run(args, config):
    if args.command == "configure":
        return configure(args, config)
    if args.command == "rom":
        return export_rom(config, args.output)
    if args.command == "off":
        return release(config)
    if args.command == "boot-init":
        return boot_init(config)
    if args.command == "power":
        return power(config, args.value)
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
            "power_readback": command_output("nvidia-smi", "-i", config["device"], "-q", "-d", "POWER"),
            "target_mw": int(path.read_text()) if path.exists() else None,
            "powerd": service_state(config, "is-active"),
            "dynamic_boost": dynamic_boost(config),
            "a61a_header": control[:20].hex(),
            "a61a_entries": policy.table(policy.GET, control),
            "a618_header": info[:0xCC].hex(),
            "a618_entries": policy.table(policy.INFO, info),
            "configured": {k: v for k, v in config.items() if k.endswith(("Watts", "Milliamps"))},
        }
        try:
            state = live_state(config, info)
            result.update({k: state[k] for k in ("verdict", "mode", "message", "rom", "bounds", "baseline")})
        except (OSError, ValueError) as exc:
            result.update(
                verdict="Unverified",
                message=f"Unverified: {exc}. Run: {REPAIR['disable']}.",
                baseline={"match": False, "error": str(exc)},
            )
            return result
        try:
            if not state["bounds"]:
                return result
            index, current_ma, low, high = policy.current_policy(
                info,
                control,
                state["rom"]["values"]["currentRatedMilliamps"],
                state["bounds"]["current"][2],
                index=state["rom"]["policies"]["current"],
                restoring=True,
            )
            result["current_candidate"] = dict(index=index, milliamps=current_ma, lower=low, upper=high)
            if state["verdict"] == "Ready":
                active = (
                    path.exists()
                    or (RUN / "limit-set").exists()
                    or current_ma != state["rom"]["values"]["currentRatedMilliamps"]
                )
                message = "Overrides active." if active else "No overrides active."
                result["message"] += " Dynamic Boost " + ("on. " if uses_powerd(config) else "off. ") + message
                result["message"] += "\nNext: sudo gpu-power " + (
                    "current max" if state["mode"] == "raised" and state["rom"]["family"] != "ada" else "power max"
                )
        except ValueError as exc:
            result["current_decode_error"] = str(exc)
        return result


def main():
    global REPAIR
    args = parser().parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("run gpu-power as root")
        config = json.loads(CONFIG.read_text())
        REPAIR = repair_commands(config)
        if args.device:
            config["device"] = args.device
        config = validate_config(config)
        RUN.mkdir(mode=0o755, exist_ok=True)
        # Honor recovery policy even if another dependency already loaded NVIDIA.
        if args.command != "off" and nvidia_blacklisted():
            raise ValueError(
                "Missing: NVIDIA is blacklisted for this recovery boot. "
                f"Run: {REPAIR['blacklist']}."
            )
        if args.command == "boot-init":
            for name in ("target-mw", "ceiling-mw"):
                (RUN / name).unlink(missing_ok=True)
            command_output("modprobe", "--use-blacklist", "nvidia")
            command_output("nvidia-smi", "-i", config["device"], "-q", "-d", "POWER")
        if args.command != "off":
            try:
                guard(config)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                raise ValueError(f"Missing: {exc}. Run: {REPAIR['reinstall']}.") from exc
        with (RUN / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = run(args, config)
            if isinstance(result, str):
                print(result)
            elif args.json or getattr(args, "full", False):
                print(json.dumps(result, indent=2))
            else:
                print(result.get("message", json.dumps(result)))
            if isinstance(result, dict) and (result.get("ok") is False or result.get("verdict", "Ready") != "Ready"):
                sys.exit(1)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        message = str(exc)
        if args.command in ("status", "boot-init") and not message.startswith(
            ("Ready", "Stale", "Missing", "Unverified")
        ):
            message = f"Unverified: {message}. Run: {REPAIR['disable']}."
        if args.command == "boot-init":
            (RUN / "ceiling-mw").unlink(missing_ok=True)
            (RUN / "target-mw").unlink(missing_ok=True)
            write_target(RUN / "notice", (message + "\n").encode())
        if args.json:
            result = {"ok": False, "message": message}
            if args.command in ("status", "boot-init"):
                result["verdict"] = message.split(":", 1)[0]
            print(json.dumps(result))
        else:
            print(message, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
