"""CPU-only orchestration checks with a fake collector; no GPU calls."""

import contextlib
import importlib.util
import io
import json
import os
import pwd
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import run as runner


def gpu(now=None, util=100, cap=125, temp=55):
    return {
        "host_epoch": str(time.time() if now is None else now),
        "utilization.gpu [%]": str(util),
        "enforced.power.limit [W]": str(cap),
        "power.max_limit [W]": "125",
        "temperature.gpu": str(temp),
    }


def platform(now=None):
    return {
        "host_epoch": str(time.time() if now is None else now),
        "ac_online": "1",
        "broker_turbo": "1",
    }


class Tests(unittest.TestCase):
    def test_load_gate(self):
        now = time.time()
        g = [gpu(now) for _ in range(5)]
        p = [platform(now)]
        self.assertEqual(len(runner.check_loaded(g, p, now)["gpu_last_five"]), 5)
        for changed in [
            [gpu(now, util=50)] * 5,
            [gpu(now, cap=115)] * 5,
            [gpu(now - 5)] * 5,
            [gpu(now, temp=85)] * 5,
        ]:
            with self.assertRaises(runner.Stop):
                runner.check_loaded(changed, p, now)
        with self.assertRaises(runner.Stop):
            runner.check_loaded(g, [dict(p[0], ac_online="0")], now)
        with self.assertRaises(runner.Stop):
            runner.check_loaded(g, [dict(p[0], broker_turbo="0")], now)
        with self.assertRaises(runner.Stop):
            runner.check_loaded(g, [platform(now - 5)], now)
        with self.assertRaises(runner.Stop):
            runner.check_gpu(gpu(util=90), idle=True)
        with self.assertRaises(runner.Stop):
            runner.check_gpu(gpu(cap=float("nan")))
        with self.assertRaises(runner.Stop):
            runner.check_gpu(
                dict(
                    gpu(), **{"clocks_event_reasons.hw_power_brake_slowdown": "Active"}
                )
            )

    def test_partial_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gpu.csv"
            p.write_text("a,b,c\n1,2,3\n4,5")
            self.assertEqual(runner.rows(p), [{"a": "1", "b": "2", "c": "3"}])

    def test_event_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.log"
            p.write_text(
                "date\t1234.5\tbench\tbench-start name=furmark res=2560x1440\n"
            )
            self.assertEqual(runner.bench_epoch(p), 1234.5)

    def test_summary_no_verdict(self):
        text = runner.summary([], None, "GET refused")
        self.assertIn("INCOMPLETE", text)
        self.assertIn("GET refused", text)
        self.assertIn("do not by themselves", text)

    def test_package_partial_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            lab = root / "lab"
            lab.mkdir()
            (lab / "policy-125").mkdir()
            for path in [
                lab / "collect.sh",
                lab / "bench.sh",
                lab / "policy-125/guard-boot.sh",
            ]:
                path.write_text("# CPU fixture only\n")

            class Fake:
                def __init__(self, out):
                    self.out = out
                    self.samples = []
                    self.collector = None

                def run(self):
                    raise runner.Stop("fixture refusal before workload")

            handlers = {
                sig: signal.getsignal(sig)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            }
            old_umask = os.umask(0o077)
            try:
                with (
                    patch.object(runner, "Sitting", Fake),
                    patch.object(runner, "LAB", lab),
                    patch.object(runner.os, "geteuid", return_value=0),
                    patch.dict(
                        os.environ, {"SUDO_USER": pwd.getpwuid(os.getuid()).pw_name}
                    ),
                    patch.object(runner.tempfile, "mkdtemp", return_value=str(out)),
                    patch.object(sys, "argv", ["run.py"]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(runner.main(), 1)
                self.assertFalse(
                    json.loads((out / "result.json").read_text())["complete"]
                )
                self.assertIn("fixture refusal", (out / "summary.txt").read_text())
                self.assertEqual((out / "summary.txt").stat().st_uid, os.getuid())
                self.assertTrue((out / "MANIFEST.sha256").exists())
            finally:
                os.umask(old_umask)
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    def test_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            childfile = Path(tmp) / "child.json"
            script = "import subprocess,time,json,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(json.dumps(p.pid)); time.sleep(60)"
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(childfile)], start_new_session=True
            )
            try:
                deadline = time.monotonic() + 3
                while not childfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                child = json.loads(childfile.read_text())
                runner.stop_owned(proc)
                self.assertIsNotNone(proc.poll())
                try:
                    state = (
                        Path("/proc/%d/stat" % child)
                        .read_text()
                        .split(") ")[1]
                        .split()[0]
                    )
                    self.assertEqual(
                        state, "Z"
                    )  # a zombie has exited; init owns reaping
                except FileNotFoundError:
                    pass
            finally:
                runner.stop_owned(proc)

    def test_command_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.command(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05
            )


class ModeAwareGpuCheck(unittest.TestCase):
    """check_gpu/check_loaded take the enforced/maximum expectation from the booted image mode."""

    def row(self, enforced, maximum, util=0, temp=40):
        return {
            "host_epoch": str(time.time()),
            "utilization.gpu [%]": str(util),
            "enforced.power.limit [W]": "%.2f" % enforced,
            "power.max_limit [W]": "%.2f" % maximum,
            "temperature.gpu": str(temp),
        }

    def test_mode3_requires_125_125(self):
        runner.check_gpu(self.row(125, 125), idle=True)
        runner.check_gpu(self.row(125, 125), idle=True, image_mode=3)
        for enforced, maximum in ((125, 140), (140, 140), (115, 125), (125, 115)):
            with (
                self.subTest(enforced=enforced, maximum=maximum),
                self.assertRaisesRegex(runner.Stop, "mode 3 runtime cap"),
            ):
                runner.check_gpu(self.row(enforced, maximum), idle=True, image_mode=3)

    def test_mode4_requires_125_enforced_140_max(self):
        # The envelope boot that stopped the first smoke run: enforced 125, max 140 (gpu-preflight.csv "3, 125.00, 140.00, 33").
        runner.check_gpu(self.row(125, 140, util=3, temp=33), idle=True, image_mode=4)
        for enforced, maximum in ((125, 125), (140, 140), (125, 150), (130, 140)):
            with (
                self.subTest(enforced=enforced, maximum=maximum),
                self.assertRaisesRegex(runner.Stop, "mode 4 runtime cap"),
            ):
                runner.check_gpu(self.row(enforced, maximum), idle=True, image_mode=4)
        with self.assertRaisesRegex(runner.Stop, "unknown image mode"):
            runner.check_gpu(self.row(125, 140), image_mode=5)
        self.assertEqual(runner.expected_limits(3), (125.0, 125.0))
        self.assertEqual(runner.expected_limits(4), (125.0, 140.0))

    def test_check_loaded_passes_mode_through(self):
        now = time.time()
        gpu = [dict(self.row(125, 140, util=100, temp=60), host_epoch=str(now))] * 5
        platform = [{"host_epoch": str(now), "ac_online": "1", "broker_turbo": "1"}]
        runner.check_loaded(gpu, platform, now, image_mode=4)
        with self.assertRaisesRegex(runner.Stop, "mode 3 runtime cap"):
            runner.check_loaded(gpu, platform, now)
        with self.assertRaisesRegex(runner.Stop, "mode 3 runtime cap"):
            runner.check_loaded(gpu, platform, now, image_mode=3)

    def test_idle_preflight_records_mode_without_furmark(self):
        for mode in (3, 4):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                sitting = runner.Sitting(out)
                with (
                    patch.object(
                        runner,
                        "command",
                        side_effect=[(0, f"policy_mode={mode}\n"), (0, "active\n")],
                    ) as commands,
                    patch.object(runner, "sha", return_value=runner.EXPECTED),
                    patch.object(runner.os, "access", return_value=True) as access,
                ):
                    sitting.preflight()
                self.assertEqual(sitting.image_mode, mode)
                self.assertEqual(
                    json.loads((out / "identity.json").read_text())["image_mode"], mode
                )
                self.assertEqual(commands.call_count, 2)
                self.assertEqual(
                    [c.args[0] for c in access.call_args_list],
                    [runner.GDB, runner.PYTHON],
                )


class SeedCheck(unittest.TestCase):
    """--idle-only verdict on synthetic A618/A619 blobs; no GDB, no hardware."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "capture_fixture", HERE / "tests/test_capture.py"
        )
        self.fx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fx)

    def blobs(self, mode, p13_runtime=99000):
        import struct

        info = bytearray(self.fx.info_blob())
        stat = bytearray(self.fx.status_blob())
        if mode == 4:
            struct.pack_into("<I", info, 0x58 + 2 * 0xE4 + 16, 140000)
            struct.pack_into("<I", info, 0x58 + 13 * 0xE4 + 16, 120000)
        off = 0x80 + 13 * 0x168C
        for field in (4, 28, 180):
            struct.pack_into("<I", stat, off + field, p13_runtime)
        struct.pack_into("<I", stat, off + 24 + 8, 99000)  # type-3 default
        stat[off + 24 + 1] = 1
        stat[off + 24 + 12] = 0xFE
        struct.pack_into("<I", stat, off + 24 + 16, p13_runtime)
        off2 = 0x80 + 2 * 0x168C
        struct.pack_into(
            "<I", stat, off2 + 180, 125000
        )  # P2 PMU output 125000 in this fixture
        return bytes(info), bytes(stat)

    def verdict(self, mode, **kw):
        info, stat = self.blobs(mode, **kw)
        with tempfile.TemporaryDirectory() as tmp:
            sitting = runner.Sitting(Path(tmp))
            sitting.image_mode = mode
            sitting.samples = [{"label": "idle", "decoded": runner.status(stat, info)}]
            return sitting.seed_verdict()

    def test_envelope_inert_seed_passes(self):
        v = self.verdict(4)
        self.assertTrue(v["p13_seed_is_rated_99000"] and v["p2_runtime_is_125000"])
        self.assertTrue(v["verdict"].startswith("PASS"), v["verdict"])
        self.assertEqual(v["p13_table_min_rated_max_mA"], [1, 99000, 120000])

    def test_envelope_seed_following_max_fails(self):
        v = self.verdict(4, p13_runtime=120000)
        self.assertFalse(v["p13_seed_is_rated_99000"])
        self.assertTrue(v["verdict"].startswith("FAIL"))

    def test_mode3_reference(self):
        v = self.verdict(3)
        self.assertIn("stock seeds confirmed", v["verdict"])

    def test_inventory_mode_must_match_boot(self):
        info, stat = self.blobs(4)
        with tempfile.TemporaryDirectory() as tmp:
            sitting = runner.Sitting(Path(tmp))
            sitting.image_mode = 3
            folder = Path(tmp) / "policies/idle/info"
            folder.mkdir(parents=True)
            (folder / "a618.bin").write_bytes(info)
            rec = dict(
                rm_callback_status="0x0",
                callback_count=1,
                buffer_guards_intact=True,
                original_a618_buffer_unchanged=True,
                reply_sha256=runner.hashlib.sha256(info).hexdigest(),
            )
            (folder / "a618.json").write_text(json.dumps(rec))

            def fake_command(args, timeout=10, env=None):
                return 0, "POLICY capture complete;\n"

            with (
                patch.object(runner, "command", side_effect=fake_command),
                patch.object(Path, "mkdir", lambda self, **k: None),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(
                    runner.Stop, "mode 4 image but the host booted mode 3"
                ):
                    sitting.snapshot("idle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
