#!/usr/bin/env python3
"""Read-only discovery shared by the preflight and installer."""

import argparse
import json
from pathlib import Path
import platform
import re
import shlex
import subprocess

GUIDE = "https://github.com/Nickdom1/gpu-power-policy/blob/main/docs/install.md"


PINS = dict(line.split("=", 1) for line in Path(__file__).with_name("pins.sh").read_text().splitlines()
            if line and not line.startswith("#"))


def output(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def release(path=Path("/etc/os-release")):
    return dict(
        (key, shlex.split(value)[0] if shlex.split(value) else "")
        for key, value in (
            line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#")
        )
    )


def packages(distro):
    if distro == "fedora":
        return output("rpm", "-qa", "--qf", "%{NAME}\n").splitlines()
    if distro == "ubuntu":
        return [
            line.split()[0]
            for line in output("dpkg-query", "-W", "-f", "${Package} ${db:Status-Status}\n").splitlines()
            if line.endswith(" installed")
        ]
    if distro == "cachyos":
        return output("pacman", "-Qq").splitlines()
    return []


def conflicting(distro, installed):
    patterns = {
        "fedora": r"^(akmod-nvidia|kmod-nvidia.*|xorg-x11-drv-nvidia.*|nvidia-driver.*|"
        r"nvidia-settings|nvidia-persistenced|nvidia-modprobe)$",
        "ubuntu": r"^(nvidia-.*|libnvidia-.*|linux-modules-nvidia-.*|xserver-xorg-video-nvidia.*)$",
        "cachyos": r"^(nvidia|nvidia-open|nvidia-dkms|nvidia-open-dkms|linux-(?!firmware(?:-|$)).*-nvidia.*)$",
    }
    return sorted(p for p in installed if re.fullmatch(patterns.get(distro, r"(?!)"), p))


def security(efi=Path("/sys/firmware/efi"), lockdown=Path("/sys/kernel/security/lockdown")):
    secure = False
    if efi.is_dir():
        variables = list((efi / "efivars").glob("SecureBoot-*"))
        try:
            raw = variables[0].read_bytes() if len(variables) == 1 else b""
        except OSError:
            raw = b""
        secure = bool(raw[4]) if len(raw) == 5 and raw[4] in (0, 1) else None
    text = read(lockdown)
    match = re.search(r"\[(none|integrity|confidentiality)\]", text)
    lock = match[1] if match else None
    return secure, lock


def inspect(kernel=None, device=None):
    distro = release()
    ident = distro.get("ID", "unknown")
    version = distro.get("VERSION_ID", "")
    kernel = kernel or platform.release()
    refusals, advice = [], []
    if platform.machine() != "x86_64":
        refusals.append("Not supported: only x86_64 is supported.")
    if not (ident in ("cachyos", "nixos") or (ident, version) in (("fedora", "43"), ("ubuntu", "24.04"))):
        reason = (
            "CachyOS is the tested Arch derivative" if ident == "arch" else "use Fedora 43, Ubuntu 24.04 or CachyOS"
        )
        refusals.append(f"Not supported: {ident} {version}; {reason}.")
    gpus = [
        p
        for p in Path("/sys/bus/pci/devices").iterdir()
        if read(p / "vendor") == "0x10de" and read(p / "class").startswith("0x03")
    ]
    if device:
        gpus = [p for p in gpus if p.name == device]
    bdf = gpus[0].name if len(gpus) == 1 else None
    if not gpus:
        refusals.append("Not supported: no selected NVIDIA display GPU.")
    elif len(gpus) != 1:
        command = (
            "nix run github:Nickdom1/gpu-power-policy#preflight --"
            if ident == "nixos" else "bash install/preflight.sh"
        )
        refusals.append(f"Blocked: multiple NVIDIA GPUs. Run: {command} --device {gpus[0].name}")
    name = output("lspci", "-s", bdf, "-nn") if bdf else "no unique GPU"
    smi = (
        output(
            "nvidia-smi",
            "-i",
            bdf,
            "--query-gpu=name,driver_version,power.default_limit,power.max_limit",
            "--format=csv,noheader",
        )
        # nvidia-smi can invoke nvidia-modprobe when the driver is absent.
        # Query only an already initialized GPU to keep preflight read-only.
        if bdf and Path(f"/proc/driver/nvidia/gpus/{bdf}").is_dir()
        else ""
    )
    description = name + " " + smi
    family = next(
        (
            family
            for pattern, family in (
                (r"\bGB\d|RTX\s*50\d\d", "blackwell"),
                (r"\bAD\d|RTX\s*40\d\d", "ada"),
                (r"\bGA\d|RTX\s*30\d\d", "ampere"),
                (r"\bTU\d|RTX\s*20\d\d|GTX\s*16\d\d", "turing"),
            )
            if re.search(pattern, description, re.IGNORECASE)
        ),
        "unknown",
    )
    mobile = bool(re.search(r"Laptop|Mobile|Max-Q|\b(?:GA|AD|TU)\d+M\b", description, re.IGNORECASE))
    boost = bdf is not None and bool(
        re.search(r"Notebook Dynamic Boost:\s+Supported", read(f"/proc/driver/nvidia/gpus/{bdf}/power"))
    )
    if bdf and not description.strip():
        command = {
            "fedora": "sudo dnf install pciutils",
            "ubuntu": "sudo apt-get install pciutils",
            "cachyos": "sudo pacman -S pciutils",
        }.get(ident, "nix shell nixpkgs#pciutils")
        refusals.append(f"Blocked: GPU identification tools are unavailable. Run: {command}, then rerun preflight.")
    elif bdf and family in ("blackwell", "unknown"):
        refusals.append(
            f"Not supported: {family} GPU family; expected Turing, Ampere or Ada. "
            "Run: lspci -nn (include this output in a support report)."
        )
    found = conflicting(ident, packages(ident))
    if found:
        command = {
            "fedora": ["sudo", "dnf", "remove", "--no-autoremove"],
            "ubuntu": ["sudo", "apt-get", "purge"],
            "cachyos": ["sudo", "pacman", "-R"],
        }[ident]
        refusals.append(
            "Blocked: a packaged NVIDIA "
            + ("kernel module" if ident == "cachyos" else "driver")
            + " is installed ("
            + ", ".join(found)
            + "). Run: "
            + shlex.join(command + found)
            + ", then run the preflight again."
        )
    if found and ident == "ubuntu":
        advice.append("Do not run apt autoremove until the project driver is installed.")
    pkgbase = read(f"/usr/lib/modules/{kernel}/pkgbase")
    headers = {
        "fedora": f"kernel-devel-{kernel}",
        "ubuntu": f"linux-headers-{kernel}",
        "cachyos": f"{pkgbase}-headers",
    }.get(ident, "managed by Nix")
    if ident != "nixos" and not Path(f"/lib/modules/{kernel}/build").is_dir():
        advice.append(f"Headers for {kernel} will be installed.")
    secure, lock = security()
    sign = lock in ("integrity", "confidentiality") or (secure is True and lock is None)
    if secure is None:
        refusals.append(
            "Blocked: Secure Boot state is unreadable. Run: "
            "sudo mount -t efivarfs efivarfs /sys/firmware/efi/efivars; rerun preflight."
        )
    if sign and ident == "cachyos":
        refusals.append(
            "Blocked: kernel lockdown is on or unavailable; this installer cannot sign for sbctl keys. "
            f"Boot with lockdown off; Run: cat /sys/kernel/security/lockdown. Guide: {GUIDE}#secure-boot"
        )
    if ident == "cachyos":
        kernels = list(Path("/usr/lib/modules").glob("*/pkgbase"))
        if not any(read(p).endswith("-lts") for p in kernels):
            advice.append("Keep a fallback: sudo pacman -S linux-cachyos-lts linux-cachyos-lts-headers")
        advice.append(
            f"The installer pins NVIDIA userspace to {PINS['version']} "
            "with IgnorePkg in /etc/pacman.conf; --remove lifts the pin."
        )
    install = (
        "follow https://github.com/Nickdom1/gpu-power-policy/blob/main/docs/install.md#install-on-nixos."
        if ident == "nixos"
        else "sudo bash install/linux.sh" + (f" --device {bdf}" if device else "") + ", then reboot."
    )
    verdict = "Supported. Run: " + install
    if family == "ada":
        verdict = (
            "Supported, stock range only. The editor cannot relocate Ada laptop images under the 512 KiB "
            "GSP limit, so raising is not available. Run: "
            if mobile
            else (
                "Supported, power only. Current control is not available on Ada; the power raise is experimental. Run: "
            )
        )
        verdict += install
    elif not mobile:
        verdict = (
            "Desktop. At stock there is nothing to gain: nvidia-smi -pl already sets any limit up to "
            "the stock maximum. Install only for raised ceilings, which are untested on desktops. Run: " + install
        )
    return {
        "distro": ident,
        "release": version,
        "kernel": kernel,
        "device": bdf,
        "family": family,
        "notebook": mobile,
        "dynamic_boost": boost,
        "gpu": name,
        "smi": smi,
        "secure_boot": secure,
        "lockdown": lock,
        "sign": sign,
        "headers": headers,
        "pkgbase": pkgbase,
        "packaged_driver": found,
        "refusals": refusals,
        "advice": advice,
        "verdict": refusals[0] if refusals else verdict,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--kernel")
    parser.add_argument("--device")
    args = parser.parse_args()
    result = inspect(args.kernel, args.device)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        secure = "unknown" if result["secure_boot"] is None else "on" if result["secure_boot"] else "off"
        signing = (
            "sign with a Machine Owner Key; enroll it at the next boot"
            if result["sign"]
            else "unsigned modules load; nothing to sign"
        )
        if result["distro"] == "nixos":
            signing = (
                "signing is your boot chain's responsibility (for example lanzaboote); this module does not sign"
            )
        elif result["sign"] and result["distro"] == "cachyos":
            signing = "blocked: sbctl module signing is not provided by this installer"
        for label, value in (
            ("GPU", result["gpu"] + f"; {result['family']}, " + ("laptop" if result["notebook"] else "desktop")),
            ("Power/driver", result["smi"] or "NVIDIA power readback unavailable; limits are read after installation"),
            ("Dynamic Boost", "on" if result["dynamic_boost"] else "off or unavailable"),
            ("Secure Boot", f"{secure}, kernel lockdown {result['lockdown'] or 'unavailable'}; {signing}"),
            ("Kernel", result["kernel"] + "; headers: " + result["headers"]),
            ("Verdict", result["verdict"]),
        ):
            print(f"{label:14}{value}")
        for line in result["refusals"][1:]:
            print(f"Also blocked: {line}")
        for line in result["advice"]:
            print(line)
    return bool(result["refusals"])


if __name__ == "__main__":
    raise SystemExit(main())
