"""CPU-only: the collector carries the runner's PT516_* settings to the workload launcher, and
ladder.py --plan works without an explicit cap. No GPU, root or network."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]


def build_user_env(extra_env, sudo_environ):
    """Run collect.sh's build_user_env as if root, with a fake sudo-process environ."""
    src = (LAB / "collect.sh").read_text()
    start = src.index("\nbuild_user_env() {")
    end = src.index("\n}\n", start) + 3
    func = src[start:end].replace("EUID", "TEST_EUID")
    with tempfile.TemporaryDirectory() as td:
        environ = Path(td) / "environ"
        environ.write_bytes(b"".join(e.encode() + b"\0" for e in sudo_environ))
        script = (
            f"TEST_EUID=0 TARGET_USER=tester TARGET_UID=1000 TARGET_HOME=/home/tester RUN_DIR= RUN_ID= LAB_DIR={LAB}\n"
            f"find_sudo_environ() {{ printf '%s\\n' '{environ}'; }}\n"
            f"{func}\nbuild_user_env\nprintf '%s\\n' \"${{USER_ENV[@]}}\"\n"
        )
        env = dict(os.environ)
        env.update(extra_env)
        out = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
        )
    return out.stdout.splitlines()


class CollectorEnvironment(unittest.TestCase):
    def test_runner_settings_reach_the_workload(self):
        lines = build_user_env(
            {
                "PT516_WITNESS_SECONDS": "600",
                "LAB_RUN_DIR": "/must/not/leak",
            },
            [
                "WAYLAND_DISPLAY=wayland-0",
                "DISPLAY=:0",
                "SUDO_USER=tester",
                "LD_PRELOAD=/x.so",
                "LAB_RUN_DIR=/stale",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
            ],
        )
        self.assertIn("PT516_WITNESS_SECONDS=600", lines)
        self.assertIn("DISPLAY=:0", lines)
        self.assertFalse(
            any(line.startswith(("SUDO_", "LD_PRELOAD=")) for line in lines)
        )
        self.assertNotIn("LAB_RUN_DIR=/stale", lines)
        self.assertNotIn("LAB_RUN_DIR=/must/not/leak", lines)

    def test_session_value_wins_over_runner_value(self):
        lines = build_user_env(
            {"PT516_X": "runner"}, ["WAYLAND_DISPLAY=w", "PT516_X=session"]
        )
        self.assertIn("PT516_X=session", lines)
        self.assertNotIn("PT516_X=runner", lines)


class LadderPlan(unittest.TestCase):
    def plan(self, *args):
        r = subprocess.run(
            [sys.executable, str(LAB / "current-limit/ladder.py"), "--plan", *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_plan_without_cap_uses_each_image_maximum(self):
        out = self.plan()
        self.assertIn("image mode 3: cap 99000 mA", out)
        self.assertIn("image mode 4: cap 120000 mA", out)

    def test_plan_hold(self):
        self.assertIn(
            "hold 107000 mA", self.plan("--hold", "107000", "--seconds", "120")
        )


if __name__ == "__main__":
    unittest.main()


class PublicEntryPoints(unittest.TestCase):
    """The documented wrappers answer --help without root, and the packaging placeholders exist."""

    def test_wrappers_help(self):
        for wrapper in ("current-limit/run.sh", "policy-status/run.sh"):
            r = subprocess.run(
                ["bash", str(LAB / wrapper), "--help"], capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0, wrapper + ": " + r.stderr)
            self.assertIn("usage:", r.stdout)

    def test_policy_status_plan(self):
        r = subprocess.run(
            [sys.executable, str(LAB / "policy-status/run.py"), "--plan"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_nvml_placeholders_for_packaging(self):
        module = LAB.parent / "module"
        for name in ("check-maximum.py", "nvml-limit.py"):
            self.assertIn("@nvml@", (module / name).read_text(), name)
        self.assertIn("--replace-fail '@nvml@'", (module / "package.nix").read_text())
