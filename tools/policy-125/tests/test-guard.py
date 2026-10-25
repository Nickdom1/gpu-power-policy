#!/usr/bin/env python3
"""Exercise the actual identity guard against synthetic host files only."""

from pathlib import Path
import os
import subprocess
import tempfile

lab = Path(__file__).resolve().parents[1]
defaults = dict(
    TEST_EUID="0",
    KERNEL="7.2.7",
    DRIVER="595.99.02",
    MODULE="FE193E9810BEB3EED2D3CE9",
    VENDOR="0x10de",
    DEVICE="0x2460",
    MODE="3",
)
cases = [
    ("positive", {}),
    ("nonroot", {"TEST_EUID": "1000"}),
    ("kernel", {"KERNEL": "7.2.8"}),
    ("driver", {"DRIVER": "596.00"}),
    ("previous-module", {"MODULE": "8DFFCA76EFF4E6157F1A90C"}),
    ("vendor", {"VENDOR": "0x8086"}),
    ("device", {"DEVICE": "0x2461"}),
    ("off", {"MODE": "0"}),
    ("control", {"MODE": "1"}),
    ("previous-mode", {"MODE": "2"}),
    ("envelope-not-allowed", {"MODE": "4", "MODULE": "CEB9B11F44C43430888F858"}),
    ("envelope-wrong-module", {"MODE": "4", "PT516_ALLOW_MODE4": "1"}),
    (
        "envelope-positive",
        {"MODE": "4", "MODULE": "CEB9B11F44C43430888F858", "PT516_ALLOW_MODE4": "1"},
    ),
    ("unknown-mode", {"MODE": "5"}),
]
positives = {"positive", "envelope-positive"}
with tempfile.TemporaryDirectory(prefix="policy-guard-fixture-") as td:
    root = Path(td)
    body = (lab / "guard-boot.sh").read_text().replace("EUID != 0", "TEST_EUID != 0")
    (root / "guard.sh").write_text("set -euo pipefail\n" + body + "\necho ACCEPTED\n")
    for name, code in {
        "uname": 'printf "%s\\n" "$KERNEL"',
        "cat": """case $1 in
  /sys/module/nvidia/version) printf '%s\\n' "$DRIVER" ;;
  /sys/module/nvidia/srcversion) printf '%s\\n' "$MODULE" ;;
  /sys/bus/pci/devices/0000:01:00.0/vendor) printf '%s\\n' "$VENDOR" ;;
  /sys/bus/pci/devices/0000:01:00.0/device) printf '%s\\n' "$DEVICE" ;;
  /sys/module/nvidia/parameters/NVreg_Pt516BootPolicy) printf '%s\\n' "$MODE" ;;
  *) echo UNEXPECTED_READ >&2; exit 77 ;;
esac""",
    }.items():
        p = root / name
        p.write_text("#!/usr/bin/env bash\n" + code + "\n")
        p.chmod(0o700)
    for name, changes in cases:
        env = dict(os.environ, **defaults)
        env.update(changes)
        env["PATH"] = str(root) + ":" + os.environ["PATH"]
        run = subprocess.run(
            ["bash", str(root / "guard.sh")], env=env, text=True, capture_output=True
        )
        assert run.returncode == (0 if name in positives else 1), (
            run.stdout + run.stderr
        )
        assert ("ACCEPTED" in run.stdout) == (name in positives), (
            run.stdout + run.stderr
        )
        assert "UNEXPECTED_READ" not in run.stderr
        print(name, "PASS")
print(
    "%d CPU host-identity guard cases passed; no host files or GPU read." % len(cases)
)
