"""CPU-only startup tests: synthetic identities and stubbed GPU/service commands."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launch

GOOD = """ROLE8 request {"requested_mW": 125000}
ROLE8 RM wrapper returned status 0x0
ROLE8 public setter returned NVML status 0
ROLE8 trace complete; one 125000 mW role-8 request
set 125.0 W: SUCCESS
after: default 80.0 W
"""
KERNEL = """PT516 boot policy command queue: 1572864 bytes
PT516 boot policy PREPARED mode=3 bytes=516608 sha256=0501a4c0f0e4c488b1b5d4be0e846330c6fa2de4e4dd610c6d9ee00c1b025d61
PT516 boot policy host registry status: 0x0
"""
KERNEL4 = KERNEL.replace(
    "mode=3 bytes=516608 sha256=0501a4c0f0e4c488b1b5d4be0e846330c6fa2de4e4dd610c6d9ee00c1b025d61",
    "mode=4 bytes=516608 sha256=57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73",
)


class Startup(unittest.TestCase):
    def fixture_identity(self, root, mode=3, srcversion=None):
        files = {
            "sys/module/nvidia/version": "595.99.02",
            "sys/module/nvidia/srcversion": srcversion
            or launch.PIN["module_srcversion"],
            "sys/module/nvidia/parameters/NVreg_Pt516BootPolicy": str(mode),
            "proc/sys/kernel/osrelease": "7.2.7",
            "proc/sys/kernel/random/boot_id": "fixture-boot",
            "sys/bus/pci/devices/0000:01:00.0/vendor": "0x10de",
            "sys/bus/pci/devices/0000:01:00.0/device": "0x2460",
        }
        for name, value in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(value + "\n")
        (root / "sys/bus/pci/devices/0000:01:00.0/driver").symlink_to(
            "/sys/bus/pci/drivers/nvidia"
        )
        return files

    def test_identity_and_mismatches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = self.fixture_identity(root)
            self.assertEqual(launch.identity(root)["mode"], 3)
            for name in files:
                if name.endswith("boot_id"):
                    continue
                old = (root / name).read_text()
                (root / name).write_text("wrong")
                with self.subTest(name=name), self.assertRaises(launch.Refused):
                    launch.identity(root)
                (root / name).write_text(old)

    def test_previous_generation_does_not_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture_identity(root)
            (root / "sys/module/nvidia/parameters/NVreg_Pt516BootPolicy").unlink()
            self.assertEqual(launch.identity(root)["mode"], "previous-boot")
            args = types.SimpleNamespace(mode=3)
            with (
                patch.object(
                    launch, "identity", return_value={"mode": "previous-boot"}
                ),
                patch.object(launch, "run_command") as cmd,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    launch.prepare(args, {"PATH": "/test"}, root), {"PATH": "/test"}
                )
                cmd.assert_not_called()

    def test_mode_mismatch_is_previous_boot_in_both_directions(self):
        # A mode-3 module under the envelope launcher, or a mode-4 module under the default
        # launcher, must run stock powerd rather than apply the other mode's expectations.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture_identity(root, mode=3)
            self.assertEqual(
                launch.identity(root, expected_mode=4)["mode"], "previous-boot"
            )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture_identity(root, mode=4, srcversion="ENVELOPEFIXTURE")
            self.assertEqual(
                launch.identity(root, expected_mode=3)["mode"], "previous-boot"
            )
            # Mode 4 without a recorded envelope srcversion is refused, never trusted.
            with (
                patch.object(
                    launch, "PIN", dict(launch.PIN, envelope_module_srcversion=None)
                ),
                self.assertRaisesRegex(launch.Refused, "not pinned"),
            ):
                launch.identity(root, expected_mode=4)
            with patch.object(
                launch,
                "PIN",
                dict(launch.PIN, envelope_module_srcversion="ENVELOPEFIXTURE"),
            ):
                self.assertEqual(launch.identity(root, expected_mode=4)["mode"], 4)
            with (
                patch.object(
                    launch, "PIN", dict(launch.PIN, envelope_module_srcversion="OTHER")
                ),
                self.assertRaises(launch.Refused),
            ):
                launch.identity(root, expected_mode=4)

    def test_missing_module_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(launch.Refused):
                launch.identity(Path(td))

    def test_vfio_or_unbound_refused_before_query(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture_identity(root)
            driver = root / "sys/bus/pci/devices/0000:01:00.0/driver"
            real_identity = launch.identity
            args = types.SimpleNamespace(mode=3)
            for owner in ("vfio-pci", None):
                driver.unlink()
                if owner:
                    driver.symlink_to("/sys/bus/pci/drivers/" + owner)
                with (
                    self.subTest(owner=owner),
                    patch.object(
                        launch,
                        "identity",
                        side_effect=lambda expected_mode: real_identity(
                            root, expected_mode
                        ),
                    ),
                    patch.object(launch, "run_command") as cmd,
                ):
                    with self.assertRaisesRegex(launch.Refused, "NVIDIA ownership"):
                        launch.prepare(args, {}, root)
                    cmd.assert_not_called()

    def test_old_environment_removed(self):
        dirty = {
            "LD_PRELOAD": "old",
            "LD_LIBRARY_PATH": "old",
            "PYTHONPATH": "old",
            "PYTHONHOME": "old",
            "NVPCF_SHIM_CTGP_MW": "99999",
            "NVPCF_SHIM_KERNEL_LIMIT_MW": "120000",
            "PATH": "/bin",
            "LANG": "C",
        }
        self.assertEqual(launch.clean_environment(dirty), {"PATH": "/bin", "LANG": "C"})

    def prepare(self, kernel=KERNEL, reply=GOOD, failure=None, mode=3):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        lib = root / "lib"
        lib.write_bytes(b"fixture")
        args = types.SimpleNamespace(
            nvml=str(lib),
            python="fake-python",
            gdb="fake-gdb",
            journalctl="fake-journal",
            shim="/fixture/shim.so",
            mode=mode,
        )
        calls = []

        def command(argv, timeout, environment):
            calls.append((argv, dict(environment)))
            self.assertNotIn("LD_PRELOAD", environment)
            if failure == len(calls):
                raise launch.Refused("fixture command failed")
            return ["constraints\n", kernel, reply][len(calls) - 1]

        pin = dict(
            launch.PIN, nvml_sha256=launch.hashlib.sha256(lib.read_bytes()).hexdigest()
        )
        with (
            patch.object(launch, "identity", return_value={"mode": mode}),
            patch.object(launch, "PIN", pin),
            patch.object(launch, "run_command", side_effect=command),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                result = launch.prepare(args, {"PATH": "/bin"}, root)
            except launch.Refused:
                return None, calls, root
        return result, calls, root

    def test_order_constraints_then_log_then_one_set(self):
        env, calls, root = self.prepare()
        self.assertEqual(
            [a[0][0] for a in calls], ["fake-python", "fake-journal", "fake-gdb"]
        )
        self.assertEqual([str(x) for x in calls[0][0][2:]], ["--max-mw", "125000"])
        self.assertEqual(calls[2][1]["PT516_POLICY_MAX_MW"], "125000")
        self.assertNotIn("PT516_POLICY_MAX_MW", env)
        self.assertEqual(env["NVPCF_SHIM_KERNEL_LIMIT_MW"], "125000")
        self.assertEqual(env["NVPCF_SHIM_KERNEL_LIMIT_CAP_MW"], "125000")
        self.assertEqual(env["LD_PRELOAD"], "/fixture/shim.so")
        self.assertTrue((root / "request.log").exists())

    def test_envelope_mode_checks_140000_but_still_requests_125000(self):
        env, calls, root = self.prepare(kernel=KERNEL4, mode=4)
        self.assertEqual([str(x) for x in calls[0][0][2:]], ["--max-mw", "140000"])
        self.assertEqual(calls[2][1]["PT516_POLICY_MAX_MW"], "140000")
        self.assertEqual(env["NVPCF_SHIM_KERNEL_LIMIT_MW"], "125000")
        self.assertEqual(env["NVPCF_SHIM_KERNEL_LIMIT_CAP_MW"], "140000")

    def test_envelope_expectations_refuse_a_mode_3_boot_log(self):
        # The envelope launcher must not accept the default image's PREPARED line, and vice versa.
        env, calls, _ = self.prepare(kernel=KERNEL, mode=4)
        self.assertIsNone(env)
        self.assertEqual(len(calls), 2)
        env, calls, _ = self.prepare(kernel=KERNEL4, mode=3)
        self.assertIsNone(env)
        self.assertEqual(len(calls), 2)

    def test_constraints_failure_prevents_set(self):
        env, calls, _ = self.prepare(failure=1)
        self.assertIsNone(env)
        self.assertEqual(len(calls), 1)

    def test_missing_or_failed_boot_evidence_prevents_set(self):
        for kernel in (
            "",
            KERNEL + "\nNVRM: Xid 31",
            KERNEL.replace("status: 0x0", "status: 0x56"),
        ):
            env, calls, _ = self.prepare(kernel=kernel)
            self.assertIsNone(env)
            self.assertEqual(len(calls), 2)

    def test_rejected_or_incomplete_set_prevents_override(self):
        for reply in (
            "",
            GOOD.replace("NVML status 0", "NVML status 2"),
            GOOD + "ROLE8 STOP: guard\n",
            GOOD.replace("status 0x0", "status 0x1f"),
            GOOD + "ROLE8 request duplicate\n",
        ):
            env, calls, root = self.prepare(reply=reply)
            self.assertIsNone(env)
            self.assertEqual(len(calls), 3)
            self.assertTrue((root / "request.log").exists())

    def test_gdb_failure_no_retry(self):
        env, calls, _ = self.prepare(failure=3)
        self.assertIsNone(env)
        self.assertEqual(len(calls), 3)

    def test_wrong_nvml_stops_before_any_query(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "lib"
            p.write_bytes(b"wrong")
            with (
                patch.object(launch, "identity", return_value={"mode": 3}),
                patch.object(launch, "run_command") as cmd,
                self.assertRaises(launch.Refused),
            ):
                launch.prepare(types.SimpleNamespace(nvml=str(p), mode=3), {}, root)
            cmd.assert_not_called()

    def test_bounded_cpu_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            launch.run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                0.02,
                os.environ.copy(),
            )

    def test_failed_command_keeps_reason(self):
        with self.assertRaisesRegex(launch.Refused, "fixture reason"):
            launch.run_command(
                [sys.executable, "-c", 'raise SystemExit("fixture reason")'],
                2,
                os.environ.copy(),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
