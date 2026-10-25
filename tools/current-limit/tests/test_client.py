#!/usr/bin/env python3
"""The real client.gdb + client.py + ladder.Client against a synthetic NVML-shaped library.

No NVIDIA library is loaded and no device is touched. The fixture's "RM" keeps a tiny
policy model so GET/SET/readback/verify/restore can be exercised end to end, including
the refusals: value above the image maximum (RM 0x1f), value outside the sitting window
(hook stops the process before any RM call), a SET not addressing policy 13, and a public
setter call with no pending operation.
"""

import importlib.util
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
LAB = TOOL.parent
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(LAB / "policy-status"))
import ladder  # noqa: E402
import p13  # noqa: E402
from codec import INFO_SIZE, STATUS_SIZE  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "capture_fixture", LAB / "policy-status/tests/test_capture.py"
)
capture_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_fixture)
AS = shutil.which("as") or "as"
LD = shutil.which("ld") or "ld"


def control_blob(limit=99000):
    raw = bytearray(p13.CONTROL_SIZE)
    raw[:16] = bytes.fromhex(
        "01000000000000000000000000fe0000"
    )  # non-zero header, +0xC = 0x00FE
    struct.pack_into("<I", raw, p13.MASK_OFFSET, p13.MASK)
    raw[p13.P13_CLASS_OFFSET] = 15
    struct.pack_into("<I", raw, p13.P13_LIMIT_OFFSET, limit)
    return bytes(raw)


def status_blob(mode):
    raw = bytearray(capture_fixture.status_blob())
    off = 0x1259C
    for field in (4, 28, 180):
        struct.pack_into("<I", raw, off + field, 99000)
    struct.pack_into("<I", raw, off + 24 + 8, 99000)
    raw[off + 24 + 1] = 1
    raw[off + 24 + 12] = 0xFE
    struct.pack_into("<I", raw, off + 24 + 16, 99000)
    raw[off + 176 + 1] = 1
    struct.pack_into("<I", raw, 0x80 + 2 * 0x168C + 180, 125000)
    return bytes(raw)


def info_blob(mode):
    raw = bytearray(capture_fixture.info_blob())
    if mode == 4:
        struct.pack_into("<I", raw, 0x58 + 2 * 0xE4 + 16, 140000)
        struct.pack_into("<I", raw, 0x58 + 13 * 0xE4 + 16, 120000)
    return bytes(raw)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="p13-client-"))
        subprocess.run(
            [AS, "--64", "-o", str(cls.tmp / "fixture.o"), str(HERE / "fixture.s")],
            check=True,
        )
        subprocess.run(
            [
                LD,
                "-m",
                "elf_x86_64",
                "-shared",
                "-Ttext=0x10000",
                "-o",
                str(cls.tmp / "fixture.so"),
                str(cls.tmp / "fixture.o"),
            ],
            check=True,
        )
        cls.so = cls.tmp / "fixture.so"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def client(self, mode, cap, fixture_max):
        out = Path(tempfile.mkdtemp(prefix="p13-out-", dir=self.tmp))
        fx = out / "fixture"
        fx.mkdir()
        (fx / "info.bin").write_bytes(info_blob(mode))
        (fx / "control.bin").write_bytes(control_blob())
        (fx / "status.bin").write_bytes(status_blob(mode))
        c = ladder.Client(
            out,
            cap,
            library=str(self.so),
            fixture=dict(
                PT516_P13_LIBRARY_SHA256=hashlib.sha256(
                    self.so.read_bytes()
                ).hexdigest(),
                PT516_P13_FIXTURE="1",
                PT516_P13_FIXTURE_DIR=str(fx),
                PT516_P13_FIXTURE_MAX=str(fixture_max),
            ),
        )
        self.addCleanup(c.close)
        return c, out

    def test_arbitrary_library_does_not_override_pin(self):
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        with self.assertRaises(ladder.Refused):
            ladder.Client(out, 99000, library=str(self.so))
        self.assertIn("NVML hash differs", (out / "client.log").read_text())

    def test_get_set_verify_restore_within_envelope(self):
        c, out = self.client(4, 114000, 120000)
        info = c.info()
        self.assertEqual(len(info), INFO_SIZE)
        original = c.get()
        self.assertEqual(p13.check_control(original), 99000)
        block = p13.build_set(original, 104000, 114000)
        status, _ = c.set(block)
        self.assertEqual(status, 0)
        self.assertEqual(c.get(), block)
        raw = c.status(info)
        self.assertEqual(len(raw), STATUS_SIZE)
        from codec import status as decode

        s, problems = p13.verify_status(decode(raw, info), 104000)
        self.assertEqual(problems, [], problems)
        status, _ = c.set(p13.build_set(c.get(), 99000, 114000))
        self.assertEqual(status, 0)
        self.assertEqual(c.get(), original)
        log = (out / "client.log").read_text()
        self.assertIn("P13 hook armed", log)
        self.assertNotIn("P13 STOP", log)
        self.assertEqual(log.count("rm_status=0x0"), 8)

    def test_image_maximum_refuses_with_0x1f_and_nothing_changes(self):
        # Mode 3 image (max 99000) but a caller asking for 104000 with a mis-set cap: RM refuses, block unchanged.
        c, out = self.client(3, 114000, 99000)
        original = c.get()
        status, _ = c.set(p13.build_set(original, 104000, 114000))
        self.assertEqual(status, 0x1F)
        self.assertEqual(c.get(), original)

    def test_hook_stops_values_outside_window_before_rm(self):
        c, out = self.client(4, 104000, 120000)
        original = c.get()
        bad = bytearray(original)
        struct.pack_into("<I", bad, p13.P13_LIMIT_OFFSET, 109000)
        with self.assertRaises(ladder.Refused):
            c.set(bytes(bad))
        log = (out / "client.log").read_text()
        self.assertIn("SET value 109000 outside [94000, 104000]", log)

    def test_hook_stops_set_not_addressing_p13(self):
        c, out = self.client(4, 114000, 120000)
        original = c.get()
        bad = bytearray(original)
        struct.pack_into("<I", bad, p13.MASK_OFFSET, 0x4)
        with self.assertRaises(ladder.Refused):
            c.set(bytes(bad))
        self.assertIn(
            "does not address policy 13 alone", (out / "client.log").read_text()
        )

    def test_codec_rules(self):
        original = control_blob()
        with self.assertRaises(p13.Refused):
            p13.build_set(original, 93000, 114000)
        with self.assertRaises(p13.Refused):
            p13.build_set(original, 115000, 114000)
        with self.assertRaises(p13.Refused):
            p13.build_set(bytes(p13.CONTROL_SIZE), 99000, 114000)
        self.assertEqual(p13.sitting_cap(4, 114000), 114000)
        with self.assertRaises(p13.Refused):
            p13.sitting_cap(3, 114000)
        with self.assertRaises(p13.Refused):
            p13.sitting_cap(4, 125000)
        steps, total = p13.ladder(114000)
        sets = [v for _, a, v in steps if a == "set"]
        self.assertEqual(sets[:9], [95000, 99000] * 4 + [99000])
        self.assertEqual(sets[-1], 99000)
        self.assertIn(104000, sets)
        self.assertIn(114000, sets)
        self.assertEqual(sets.count(104000), 2)
        self.assertLessEqual(total, 600)
        steps3, _ = p13.ladder(99000)
        self.assertFalse(any(v > 99000 for _, a, v in steps3 if a == "set"))


class SittingTests(ClientTests):
    """smoke() and analyze() through the real Sitting with the fixture client; preflight is stubbed."""

    def sitting(self, mode=4, cap=114000, fixture_max=120000):
        out = Path(tempfile.mkdtemp(prefix="p13-sit-", dir=self.tmp))
        fx = out / "fixture"
        fx.mkdir()
        (fx / "info.bin").write_bytes(info_blob(mode))
        (fx / "control.bin").write_bytes(control_blob())
        (fx / "status.bin").write_bytes(status_blob(mode))
        s = ladder.Sitting(out, cap, (104000, 109000, 114000), 600)
        s.image_mode = mode
        s.cap = p13.sitting_cap(mode, min(cap, p13.IMAGE_MAX_MA[mode]))
        s.schedule, s.workload_s = p13.ladder(s.cap, s.rungs)
        (out / "identity.json").write_text(json.dumps(dict(boot_id="fixture")))
        s.preflight = lambda: None
        real_client = ladder.Client
        so, fxenv = (
            self.so,
            dict(
                PT516_P13_LIBRARY_SHA256=hashlib.sha256(
                    self.so.read_bytes()
                ).hexdigest(),
                PT516_P13_FIXTURE="1",
                PT516_P13_FIXTURE_DIR=str(fx),
                PT516_P13_FIXTURE_MAX=str(fixture_max),
            ),
        )
        ladder.Client = lambda out_, cap_, library=None, fixture=None: real_client(
            out_, cap_, library=str(so), fixture=fxenv
        )
        self.addCleanup(setattr, ladder, "Client", real_client)
        self.addCleanup(lambda: s.client and s.client.close())
        return s, out

    def test_smoke_identity_set_only(self):
        s, out = self.sitting()
        import io
        import contextlib

        with contextlib.redirect_stdout(io.StringIO()):
            result = s.smoke()
        self.assertEqual(s.restore_check, "a61a+a619")
        self.assertEqual(result["identity_set_rm_status"], 0)
        self.assertEqual(result["p13"]["limit"], 99000)
        self.assertEqual(result["p13"]["table_max"], 120000)
        self.assertFalse(s.dirty)
        text = s.report(result)
        self.assertIn(
            "SMOKE: image mode 4, cap 114000 mA, identity E61B SET rm_status=0x0", text
        )
        # Exactly one SET and it wrote 99000.
        ops = sorted((out / "rm-ops").glob("op-*.result.json"))
        kinds = [json.loads(p.read_text())["kind"] for p in ops]
        self.assertEqual(kinds.count("set"), 1)

    def test_write_restore_and_refusal_mark_dirty_correctly(self):
        s, out = self.sitting()
        import io
        import contextlib

        with contextlib.redirect_stdout(io.StringIO()):
            s.setup()
            s.write(104000)
            self.assertTrue(s.dirty)
            self.assertEqual(s.current, 104000)
            s.restore()
            self.assertFalse(s.dirty)
            self.assertEqual(s.client.get(), s.original)

    def test_last_resort_restore_when_live_block_changed_elsewhere(self):
        s, out = self.sitting()
        import io
        import contextlib

        with contextlib.redirect_stdout(io.StringIO()):
            s.setup()
            s.write(104000)
            self.assertTrue(s.dirty)
            real_get, real_close = s.client.get, s.client.close
            perturbed = bytearray(real_get())
            perturbed[8] ^= 1  # a byte outside the limit word changed since setup
            calls = {"n": 0}

            def once(*a, **k):
                calls["n"] += 1
                return bytes(perturbed) if calls["n"] == 1 else real_get(*a, **k)

            s.client.get = once
            s.client.close = lambda: None  # keep the process for the assertions below
            s.cleanup()
            self.assertFalse(s.dirty)
            self.assertEqual(s.current, p13.RATED_MA)
            self.assertEqual(real_get(), s.original)
            real_close()
        texts = [e["text"] for e in s.events]
        self.assertIn(
            "last-resort restore: A61A readback matched; A619 not checked", texts
        )
        self.assertEqual(s.restore_check, "a61a-only")
        self.assertTrue(any(f.startswith("RESTORATION:") for f in s.failures))
        self.assertFalse(any(f.startswith("LAST-RESORT") for f in s.failures))

    def test_analysis_windows_and_verdicts(self):
        s, out = self.sitting()
        s.begin = 1000.0
        tele = s.telemetry
        (tele / "workload").mkdir(parents=True)
        # Synthetic telemetry: 580 s at 1 Hz. SM/power follow the P13 setting in force.
        sets = [(at, v) for at, a, v in s.schedule if a == "set"]

        def setting(t):
            cur = 99000
            for at, v in sets:
                if t >= at:
                    cur = v
            return cur

        def model(ma):
            # 95k: -34 MHz -1.5% fps; 104k: +42 MHz +1.8%; >=107k co-bound at P2: flat +3%
            if ma == 95000:
                return 768, 115.0, 87.7
            if ma == 99000:
                return 802, 117.9, 89.0
            if ma == 104000:
                return 844, 121.5, 90.6
            return 870, 124.6, 91.7

        rows = [
            "host_epoch,utilization.gpu [%],enforced.power.limit [W],power.max_limit [W],temperature.gpu,clocks.current.sm [MHz],power.draw [W]"
        ]
        for t in range(0, 581):
            sm, pw, _ = model(setting(t))
            rows.append("%f,100,125,125,55,%d,%f" % (1000.0 + t, sm, pw))
        (tele / "gpu.csv").write_text("\n".join(rows) + "\n")
        for t in range(20, 581, 4):
            ma = setting(t)
            s.snapshots.append(
                dict(
                    label="cadence",
                    elapsed=float(t),
                    epoch=1000.0 + t,
                    p13=dict(
                        reading=min(ma, 107500) - 1200, gsp_type3_output=ma, limit=ma
                    ),
                    clock_comparison=dict(
                        exact_matches=[[12] if ma < 107000 else [0], []]
                    ),
                    p2=dict(reading_hint_raw=int(model(ma)[1] * 1000)),
                    p12_domain_limits=None,
                    p0_domain_limits=None,
                )
            )
        (tele / "workload/furmark-furmark.log").write_text(
            FURMARK_LOG % ("NVIDIA GeForce RTX 3080 Ti Laptop GPU", 53771, 600000)
        )
        analysis = s.analyze()
        v = analysis["verdicts"]
        self.assertLess(v["pulse_sm_pct"], -1.0)
        self.assertGreater(v["up104"]["sm_pct"], 1.0)
        self.assertFalse(v["up104"]["co_bound"])
        self.assertTrue(v["up109"]["co_bound"])
        self.assertTrue(v["up114"]["co_bound"])
        text = s.report(analysis)
        self.assertIn("up114", text)
        self.assertIn("pulse_sm_pct", text)


class GuardAndReportTests(unittest.TestCase):
    """Mode-aware GPU guard in the ladder, and the summary wording when nothing was written."""

    def sitting(self):
        out = Path(tempfile.mkdtemp(prefix="p13-guard-"))
        self.addCleanup(shutil.rmtree, out, True)
        s = ladder.Sitting(out, 114000, (104000, 109000, 114000), 600)
        s.image_mode = 4
        s.telemetry.mkdir()
        return s

    def gpu_csv(self, s, enforced, maximum):
        import time as _t

        now = _t.time()
        (s.telemetry / "gpu.csv").write_text(
            "host_epoch,utilization.gpu [%],enforced.power.limit [W],power.max_limit [W],temperature.gpu,clocks.current.sm [MHz],power.draw [W]\n"
            + "".join(
                "%f,100,%d,%d,60,800,118\n" % (now - 4 + i, enforced, maximum)
                for i in range(5)
            )
        )
        (s.telemetry / "platform.csv").write_text(
            "host_epoch,ac_online,broker_turbo\n%f,1,1\n" % now
        )
        (s.telemetry / "journal.log").write_text("")

    def test_guard_accepts_envelope_limits_and_refuses_mode3_reading_on_mode4(self):
        s = self.sitting()
        self.gpu_csv(s, 125, 140)
        s.guard()  # the reading that stopped the first smoke run, now accepted on mode 4
        self.gpu_csv(s, 125, 125)
        with self.assertRaisesRegex(ladder.policy.Stop, "mode 4 runtime cap"):
            s.guard()  # a mode-3 image under a mode-4 sitting is refused, not "any max"
        s.image_mode = 3
        s.guard()
        self.gpu_csv(s, 125, 140)
        with self.assertRaisesRegex(ladder.policy.Stop, "mode 3 runtime cap"):
            s.guard()

    def test_preflight_gpu_check_uses_booted_mode(self):
        row = dict(
            zip(
                [
                    "utilization.gpu [%]",
                    "enforced.power.limit [W]",
                    "power.max_limit [W]",
                    "temperature.gpu",
                ],
                ["3", "125.00", "140.00", "33"],
            )
        )  # captured mode-4 GPU preflight values
        ladder.policy.check_gpu(row, idle=True, image_mode=4)
        with self.assertRaisesRegex(ladder.policy.Stop, "mode 3 runtime cap"):
            ladder.policy.check_gpu(row, idle=True, image_mode=3)

    def test_report_says_not_touched_when_no_set_was_issued(self):
        s = self.sitting()
        s.failures.append("Stop: fixture preflight refusal")
        text = s.report(None)
        self.assertIn("P13 not touched: no E61B SET was issued.", text)
        self.assertNotIn("restored", text.lower())
        s.sets_issued = 1
        s.restore_check = "a61a+a619"
        self.assertIn("P13 restore: a61a+a619", s.report(None))
        s.dirty = True
        s.restore_check = "failed"
        self.assertIn("P13 restore: failed", s.report(None))


FURMARK_LOG = """(17:03:18)\tFurMark 2.10.2.0
(17:13:23)\t- renderer             : %s
(17:13:23)\t- SCORE                : %d
(17:13:23)\t- duration             : %d ms
(17:13:23)\t- FPS (min/avg/max)    : 54 / 90 / 98
"""


class WitnessAndHoldTests(unittest.TestCase):
    """Renderer/placement/frame-log witnesses (issues 2-4 of 2026-10-24) and the --hold ABBA mode."""

    def sitting(self, hold=None, seconds=120):
        out = Path(tempfile.mkdtemp(prefix="p13-wit-"))
        self.addCleanup(shutil.rmtree, out, True)
        s = ladder.Sitting(out, 114000, (104000, 109000, 114000), seconds, hold_ma=hold)
        s.image_mode = 4
        s.cap = 114000
        if hold is not None:
            s.schedule, s.workload_s = p13.hold_schedule(hold, s.cap, seconds)
        else:
            s.schedule, s.workload_s = p13.ladder(s.cap, s.rungs)
        (s.telemetry / "workload").mkdir(parents=True)
        s.begin = 1000.0
        return s

    def witness(self, s, panel=True):
        w = s.telemetry / "witness/geometry-30s"
        w.mkdir(parents=True)
        displays = [
            dict(
                name="eDP-1",
                pos=dict(x=0, y=0),
                scale=1.25,
                rotation=1,
                size=dict(width=2560, height=1600),
                refresh=165.0,
            ),
            dict(
                name="DP-9",
                pos=dict(x=2048, y=0),
                scale=1.0,
                rotation=1,
                size=dict(width=2560, height=1440),
                refresh=120.0,
            ),
        ]
        geom = (
            dict(x=0, y=0, width=2048, height=1280)
            if panel
            else dict(x=2048, y=0, width=2560, height=1440)
        )
        (w / "result.json").write_text(
            json.dumps(
                dict(window=dict(fullscreen=True, geometry=geom), displays=displays)
            )
        )

    def test_hold_schedule(self):
        steps, total = p13.hold_schedule(107000, 114000, 120)
        sets = [(at, v) for at, a, v in steps if a == "set"]
        self.assertEqual(sets, [(5, 107000), (112, 99000)])
        self.assertEqual(total, 118)
        with self.assertRaises(p13.Refused):
            p13.hold_schedule(115000, 114000, 120)
        steps99, _ = p13.hold_schedule(
            99000, 114000, 120
        )  # identity hold keeps the op count equal in ABBA
        self.assertEqual([v for _, a, v in steps99 if a == "set"], [99000, 99000])

    def test_placement_refuses_external_output(self):
        s = self.sitting()
        self.witness(s, panel=False)
        with self.assertRaises(ladder.policy.Stop):
            s.check_placement(40.0)
        self.assertFalse(s.witness_checked)

    def test_placement_requires_witness_by_36s(self):
        s = self.sitting()
        with self.assertRaisesRegex(ladder.Refused, "placement unverified"):
            s.check_placement(40.0)
        s.check_placement(30.0)  # too early: no decision yet
        self.assertFalse(s.witness_checked)

    def test_placement_and_renderer_pass_on_panel(self):
        s = self.sitting()
        self.witness(s, panel=True)
        (s.telemetry / "workload/furmark-furmark.log").write_text(
            FURMARK_LOG % ("NVIDIA GeForce RTX 3080 Ti Laptop GPU", 10634, 120000)
        )
        s.check_placement(40.0)
        self.assertTrue(s.witness_checked)
        self.assertEqual(s.renderer, "NVIDIA GeForce RTX 3080 Ti Laptop GPU")

    def test_renderer_refuses_intel(self):
        s = self.sitting()
        (s.telemetry / "workload/furmark-furmark.log").write_text(
            FURMARK_LOG % ("Intel(R) Graphics (ADL GT2)", 3000, 120000)
        )
        with self.assertRaisesRegex(ladder.Refused, "wrong GPU"):
            s.check_renderer()

    def test_hold_analysis_uses_score(self):
        s = self.sitting(hold=107000)
        (s.telemetry / "workload/furmark-furmark.log").write_text(
            FURMARK_LOG % ("NVIDIA GeForce RTX 3080 Ti Laptop GPU", 10900, 120000)
        )
        (s.telemetry / "gpu.csv").write_text(
            "host_epoch,utilization.gpu [%],enforced.power.limit [W],power.max_limit [W],temperature.gpu,clocks.current.sm [MHz],power.draw [W]\n"
            + "".join(
                "%f,100,125,140,60,900,124.5\n" % (1000.0 + t) for t in range(120)
            )
        )
        s.snapshots = [
            dict(
                label="hold",
                elapsed=float(t),
                epoch=1000.0 + t,
                p13=dict(reading=105500, gsp_type3_output=107000, limit=107000),
                clock_comparison=dict(exact_matches=[[0], []]),
                p2=dict(reading_hint_raw=124800),
            )
            for t in range(20, 110, 20)
        ]
        a = s.analyze()
        self.assertEqual(a["score"], 10900)
        self.assertAlmostEqual(a["score_per_second"], 90.833, places=2)
        self.assertEqual(a["p13_output_ma"], [107000])
        text = s.report(a)
        self.assertIn("HOLD 107000 mA: FurMark SCORE 10900", text)
        self.assertIn("One arm of an ABBA", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
