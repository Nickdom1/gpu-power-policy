"""Staged acceptance test inside a disposable GPU VM; never reboots itself.

sudo python3 tests/first_run_guest.py pristine|bootstrap|raised|disabled|removed
Reboot between stages. Bootstrap deliberately stages a +8% current ceiling on
Turing/Ampere; Ada desktops use +5% power. Ada laptops stay at stock.
Desktop setup, Secure Boot enrollment and kernel-update tests are separate gates.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path("/var/lib/gpu-power-first-run-test")
REPO = Path(__file__).resolve().parents[1]
BOOT = Path("/proc/sys/kernel/random/boot_id")
CONFIG = Path("/etc/gpu-power-policy.json")


def run(*command):
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    result.check_returncode()
    return result.stdout


def control(*arguments):
    return json.loads(run("gpu-power", "--json", *arguments))


def boot_after(previous):
    if BOOT.read_text() == (ROOT / (previous + ".boot")).read_text():
        raise RuntimeError("reboot the guest before this stage")


def mark(stage):
    (ROOT / (stage + ".boot")).write_text(BOOT.read_text())
    print("PASS " + stage + "; reboot the guest before the next stage", flush=True)


def check_refusals():
    original = CONFIG.read_bytes()
    config = json.loads(original)
    try:
        for verdict, change in (
            ("Stale", lambda c: c["romBaseline"].update(sha256="0" * 64)),
            ("Unverified", lambda c: c.update(powerCeilingWatts=c["powerCeilingWatts"] + 1)),
        ):
            altered = json.loads(original)
            change(altered)
            CONFIG.write_text(json.dumps(altered))
            result = subprocess.run(["gpu-power", "status", "--json"], capture_output=True, text=True)
            state = json.loads(result.stdout)
            assert result.returncode != 0 and state["verdict"] == verdict, state
            before = Path("/run/gpu-power-policy/target-mw")
            assert not before.exists()
            refused = subprocess.run(["gpu-power", "current", "max"], capture_output=True, text=True)
            if config["romBaseline"]["family"] != "ada":
                assert refused.returncode != 0 and verdict in refused.stderr, refused.stderr
            assert not before.exists()
    finally:
        CONFIG.write_bytes(original)


def main(stage):
    if os.geteuid() != 0 or subprocess.run(["systemd-detect-virt", "--vm", "--quiet"]).returncode:
        raise RuntimeError("run as root inside a disposable GPU-passthrough VM")
    if stage == "pristine":
        for name in (
            str(CONFIG),
            "/etc/gpu-power-policy-install.json",
            "/usr/local/bin/gpu-power",
            "/var/cache/gpu-power-policy",
            "/var/lib/gpu-power-policy",
            "/sys/module/nvidia",
            str(ROOT),
        ):
            if Path(name).exists():
                raise RuntimeError("not a clean first-run guest: " + name)
        run("bash", str(REPO / "install/preflight.sh"))
        ROOT.mkdir(mode=0o700)
        run("uname", "-a")
        print(Path("/etc/os-release").read_text())
        run("bash", str(REPO / "install/linux.sh"))
        mark(stage)
    elif stage == "bootstrap":
        boot_after("pristine")
        before = CONFIG.read_bytes()
        status = control("status")
        assert status["verdict"] == "Ready" and status["mode"] == "stock", status
        assert status["baseline"]["match"] and status["baseline"]["seeds_match"]
        assert status["image"]["state"] != "selected"
        config = control("configure")
        assert config["vbios"] is None and config["powerdTargetWatts"] is None
        assert not Path("/etc/modprobe.d/gpu-power-policy.conf").exists()
        assert CONFIG.read_bytes() == before
        control("power", "max")
        if status["rom"]["family"] != "ada":
            current = control("current", "max")
            assert not current["changed"]
        assert control("off")["ok"]
        (ROOT / "stock-status.json").write_text(json.dumps(status, indent=2))
        if status["rom"]["family"] == "ada" and status["rom"].get("notebook"):
            flags = []
        else:
            flags = ["--power-ceiling", "+5%"] if status["rom"]["family"] == "ada" else ["--current-ceiling", "+8%"]
        run("bash", str(REPO / "install/linux.sh"), *flags)
        saved = json.loads(CONFIG.read_text())
        if saved.get("vbios"):
            rom = Path(saved["vbios"])
            assert rom.stat().st_mode & 0o777 == 0o644
            assert rom.parent.stat().st_mode & 0o777 == 0o755
            assert rom.parent.parent.stat().st_mode & 0o777 == 0o755
            assert hashlib.sha256(rom.read_bytes()).hexdigest() == rom.stem
        mark(stage)
    elif stage == "raised":
        boot_after("bootstrap")
        status = control("status")
        assert status["verdict"] == "Ready", status
        if json.loads(CONFIG.read_text()).get("output_sha256"):
            assert status["mode"] == "raised" and status["image"]["state"] == "selected"
            check_refusals()
        assert status["baseline"]["match"] and status["baseline"]["seeds_match"]
        assert status["target_mw"] is None
        if status["rom"]["family"] != "ada":
            control("current", "max")
        control("power", "max")
        assert control("off")["ok"]
        assert not Path("/run/gpu-power-policy/limit-set").exists()
        (ROOT / "raised-status.json").write_text(json.dumps(status, indent=2))
        run("bash", str(REPO / "install/linux.sh"), "--disable")
        assert Path("/usr/local/bin/gpu-power").exists()
        assert not Path("/etc/modprobe.d/gpu-power-policy.conf").exists()
        mark(stage)
    elif stage == "disabled":
        boot_after("raised")
        status = control("status")
        assert status["verdict"] == "Ready" and status["mode"] == "stock", status
        run("bash", str(REPO / "install/linux.sh"), "--remove")
        for name in (
            str(CONFIG),
            "/usr/local/bin/gpu-power",
            "/var/cache/gpu-power-policy",
            "/etc/modprobe.d/gpu-power-policy.conf",
            "/etc/modprobe.d/gpu-power-policy-nouveau.conf",
            "/etc/kernel/install.d/40-gpu-power-policy.install",
            "/etc/kernel/install.d/60-gpu-power-policy.install",
            "/etc/kernel/postinst.d/zz-gpu-power-policy",
            "/etc/kernel/postinst.d/gpu-power-policy",
            "/etc/pacman.d/hooks/85-gpu-power-policy.hook",
        ):
            assert not Path(name).exists(), name
        mark(stage)
    elif stage == "removed":
        boot_after("disabled")
        assert not Path("/usr/local/bin/gpu-power").exists()
        assert subprocess.run(["systemctl", "is-enabled", "--quiet", "nvidia-powerd"]).returncode != 0
        assert subprocess.run(["systemctl", "is-active", "--quiet", "nvidia-powerd"]).returncode != 0
        print("PASS first-run, stock controls, ceiling raise, refusal checks, disable, removal and post-removal boot")
    else:
        raise ValueError(__doc__)


if __name__ == "__main__":
    main(sys.argv[1])
