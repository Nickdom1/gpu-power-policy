#!/usr/bin/env python3
"""P13 ladder: causal pulses, a null write and the first rungs above stock in one panel FurMark run, with SM clock and policy readings per window.

The schedule and the analysis rules are in p13.py.
Writes exactly: E61B SETs of policy 13's 0xFE input, each built from the live A61A GET, each
bounded by [94000, cap]; cap = min(sitting cap, booted image maximum). Scheduled restores
run under load and are verified with A61A and A619. Cleanup stops the workload and
attempts to restore 99000; failures are reported.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
POLICY = LAB / "policy-status"
sys.path.insert(0, str(POLICY))
sys.path.insert(0, str(HERE))
import p13  # noqa: E402
import run as policy  # noqa: E402  (preflight, telemetry helpers, codec)
from codec import EXPECTED as NVML_SHA  # noqa: E402
from codec import inventory
from codec import request as status_request
from codec import status as decode_status
from series import panel_geometry, validate_witness  # noqa: E402

GDB = policy.GDB
PYTHON = policy.PYTHON
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
STEP_TIMEOUT = 12


class Refused(RuntimeError):
    pass


def save(path, value):
    data = (
        value
        if isinstance(value, bytes)
        else (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    )
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


class Client:
    """One GDB-hooked NVML process for the whole sitting; one operation at a time."""

    def __init__(
        self,
        out,
        cap_ma,
        library=os.environ.get(
            "PT516_P13_LIBRARY", "/run/opengl-driver/lib/libnvidia-ml.so.1"
        ),
        fixture=None,
    ):
        self.ops = out / "rm-ops"
        self.ops.mkdir()
        self.n = 0
        self.proc = None
        self.cap_ma = cap_ma
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
        }
        env.update(
            PT516_P13_OPS=str(self.ops),
            PT516_P13_LIBRARY=library,
            PT516_P13_CAP_MA=str(cap_ma),
            PT516_P13_LIBRARY_SHA256=NVML_SHA,
            PYTHONDONTWRITEBYTECODE="1",
        )
        if fixture:
            env.update(fixture)
        self.log = (out / "client.log").open("w")
        self.proc = subprocess.Popen(
            [
                str(GDB),
                "--nx",
                "--batch",
                "-x",
                str(HERE / "client.gdb"),
                "--args",
                str(PYTHON),
                str(HERE / "client.py"),
            ],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not (self.ops / "client-ready.json").exists():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise Refused("P13 client did not start; see client.log")
            time.sleep(0.05)

    def op(self, kind, request):
        if self.proc.poll() is not None:
            raise Refused("P13 client exited")
        n = self.n
        self.n += 1
        save(
            self.ops / ("op-%04d.json" % n), dict(kind=kind, request_hex=request.hex())
        )
        result = self.ops / ("op-%04d.result.json" % n)
        deadline = time.monotonic() + STEP_TIMEOUT
        while not result.exists():
            if self.proc.poll() is not None:
                raise Refused(
                    "P13 client stopped during %s op %d; see client.log" % (kind, n)
                )
            if time.monotonic() > deadline:
                raise Refused("%s op %d timed out; no retry" % (kind, n))
            time.sleep(0.02)
        rec = json.loads(result.read_text())
        reply = (self.ops / ("op-%04d.reply.bin" % n)).read_bytes()
        if not rec["guards_intact"]:
            raise Refused("buffer guard failed on %s op %d" % (kind, n))
        return rec, reply

    def info(self):
        rec, raw = self.op("info", bytes(0x21B8))
        if rec["rm_status"]:
            raise Refused("A618 rm status 0x%x" % rec["rm_status"])
        return raw

    def get(self):
        rec, raw = self.op("get", p13.request())
        if rec["rm_status"]:
            raise Refused("A61A rm status 0x%x" % rec["rm_status"])
        p13.check_control(raw)
        return raw

    def status(self, info):
        _, req = status_request("status", info)
        rec, raw = self.op("status", req)
        if rec["rm_status"]:
            raise Refused("A619 rm status 0x%x" % rec["rm_status"])
        return raw

    def set(self, block):
        rec, raw = self.op("set", block)
        return rec["rm_status"], raw

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                save(self.ops / ("op-%04d.json" % self.n), dict(kind="quit"))
                self.proc.wait(timeout=10)
            except Exception:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()


class Sitting:
    def __init__(self, out, cap_ma, rungs, seconds, hold_ma=None, smoke=False):
        self.out = out
        self.cap_request = cap_ma
        self.rungs = rungs
        self.seconds = seconds
        # --hold: one whole run at a fixed P13 setting (ABBA by FurMark score); None = the ladder
        self.hold_ma = hold_ma
        self.is_smoke = smoke
        self.restore_check = None
        self.renderer = None
        self.witness_checked = False
        self.client = None
        self.collector = None
        self.info = None
        self.original = None
        self.current = p13.RATED_MA
        self.dirty = False
        self.sets_issued = 0
        self.events = []
        self.snapshots = []
        self.failures = []
        self.begin = None
        self.image_mode = None
        self.telemetry = out / "p13-ladder"

    # ---- setup
    def preflight(self):
        sitting = policy.Sitting(self.out)
        sitting.preflight()  # guard-boot (mode 3 or 4), NVML hash, powerd active
        self.image_mode = sitting.image_mode
        self.cap = p13.sitting_cap(
            self.image_mode,
            self.cap_request
            if self.cap_request is not None
            else p13.IMAGE_MAX_MA[self.image_mode],
        )
        if self.is_smoke:
            self.schedule, self.workload_s = [], 0
        elif self.hold_ma is not None:
            self.schedule, self.workload_s = p13.hold_schedule(
                self.hold_ma, self.cap, self.seconds
            )
        else:
            self.schedule, self.workload_s = p13.ladder(self.cap, self.rungs)
        if self.workload_s > self.seconds:
            raise Refused(
                "ladder needs %d s of workload but --seconds is %d"
                % (self.workload_s, self.seconds)
            )
        try:
            policy.power_preflight(self.out)
        except policy.Stop as exc:
            raise Refused(str(exc))
        for p in (GDB, PYTHON) + (
            () if self.is_smoke else (LAB / ".tools/furmark/bin/furmark",)
        ):
            if not os.access(p, os.X_OK):
                raise Refused("prepared executable unavailable: %s" % p)
        for p in Path("/proc").glob("[0-9]*/comm"):
            try:
                if p.read_text().strip().lower().startswith("furmark"):
                    raise Refused("another FurMark is running")
            except OSError:
                pass
        fields = [
            "utilization.gpu",
            "enforced.power.limit",
            "power.max_limit",
            "temperature.gpu",
        ]
        rc, text = policy.command(
            [
                "nvidia-smi",
                "-i",
                "0000:01:00.0",
                "--query-gpu=" + ",".join(fields),
                "--format=csv,noheader,nounits",
            ]
        )
        (self.out / "gpu-preflight.csv").write_text(text)
        parsed = [v.strip() for v in text.strip().split(",")]
        if rc or len(parsed) != 4:
            raise Refused("GPU preflight query incomplete")
        policy.check_gpu(
            dict(
                zip(
                    [
                        "utilization.gpu [%]",
                        "enforced.power.limit [W]",
                        "power.max_limit [W]",
                        "temperature.gpu",
                    ],
                    parsed,
                )
            ),
            idle=True,
            image_mode=self.image_mode,
        )
        save(
            self.out / "identity.json",
            dict(
                boot_id=sitting.boot,
                image_mode=self.image_mode,
                cap_ma=self.cap,
                floor_ma=p13.FLOOR_MA,
                rungs_ma=[r for r in self.rungs if r <= self.cap],
                nvml_sha256=NVML_SHA,
                schedule=self.schedule,
                workload_seconds=0 if self.is_smoke else self.seconds,
                design=(
                    "smoke"
                    if self.is_smoke
                    else "p13 ladder: pulses, null, rungs"
                    if self.hold_ma is None
                    else "p13 hold %d mA (ABBA arm)" % self.hold_ma
                ),
                hold_ma=self.hold_ma,
                writes="E61B policy-13 0xFE input only; restore 99000 under load",
            ),
        )

    def setup(self):
        self.preflight()
        self.client = Client(self.out, self.cap)
        self.info = self.client.info()
        inv = inventory(self.info)
        if inv["image_mode"] != self.image_mode:
            raise Refused(
                "A618 image mode %d differs from booted mode %d"
                % (inv["image_mode"], self.image_mode)
            )
        self.original = self.client.get()
        p13.check_control(self.original, expect_limit=p13.RATED_MA)
        save(self.out / "original-control.bin", self.original)
        save(
            self.out / "recovery.json",
            dict(
                boot_id=json.loads((self.out / "identity.json").read_text())["boot_id"],
                original_sha256=hashlib.sha256(self.original).hexdigest(),
                rated_ma=p13.RATED_MA,
                cap_ma=self.cap,
            ),
        )
        self.snapshot("idle", expect=p13.RATED_MA)
        print(
            "P13 original 99000 mA; table max %d mA; sitting cap %d mA; %d SET operations planned."
            % (
                p13.status_p13(self.snapshots[-1]["decoded"])["table_max"],
                self.cap,
                sum(1 for s in self.schedule if s[1] == "set"),
            ),
            flush=True,
        )

    # ---- primitives
    def snapshot(self, label, expect=None):
        raw = self.client.status(self.info)
        decoded = decode_status(raw, self.info)
        rec = dict(
            label=label,
            elapsed=self.elapsed(),
            epoch=time.time(),
            p13=p13.status_p13(decoded),
            clock_comparison=decoded["clock_comparison"],
            p2={
                k: next(p for p in decoded["policies"] if p["index"] == 2)[k]
                for k in (
                    "reading_hint_raw",
                    "limit_hint_raw",
                    "gsp_type3_output_raw",
                    "pmu_arbiter_output_raw",
                )
            },
            p12_domain_limits=next(
                p for p in decoded["policies"] if p["index"] == 12
            ).get("domain_limits_raw"),
            p0_domain_limits=next(
                p for p in decoded["policies"] if p["index"] == 0
            ).get("domain_limits_raw"),
            reply_sha256=hashlib.sha256(raw).hexdigest(),
            decoded=decoded,
        )
        folder = self.out / "snapshots"
        folder.mkdir(exist_ok=True)
        save(folder / ("%03d-%s.bin" % (len(self.snapshots), label)), raw)
        self.snapshots.append(rec)
        save(
            self.out / "snapshots.jsonl",
            "".join(
                json.dumps({k: v for k, v in s.items() if k != "decoded"}) + "\n"
                for s in self.snapshots
            ).encode(),
        )
        if expect is not None:
            s, problems = p13.verify_status(decoded, expect)
            if problems:
                raise Refused(
                    "A619 after %s does not show %d: %s"
                    % (label, expect, "; ".join(problems))
                )
        return rec

    def write(self, value):
        """GET → edit +0x970 → SET → GET readback → A619 verify. One attempt."""
        before = self.client.get()
        if (
            not p13.same_except_limit(before, self.original)
            or before[:16] != self.original[:16]
        ):
            raise Refused(
                "live A61A block differs from the original outside the limit word; no SET"
            )
        block = p13.build_set(before, value, self.cap)
        self.dirty = value != p13.RATED_MA or self.dirty
        self.restore_check = "failed"
        self.event("set %d" % value)
        self.sets_issued += 1
        status, _ = self.client.set(block)
        if status:
            self.event("set %d refused rm 0x%x" % (value, status))
            raise Refused("E61B SET %d returned RM 0x%x" % (value, status))
        after = self.client.get()
        if after != block:
            raise Refused("A61A readback differs from the written block")
        self.current = value
        time.sleep(1.0)
        self.snapshot("after-set-%d" % value, expect=value)
        if value == p13.RATED_MA:
            self.dirty = False
            self.restore_check = "a61a+a619"

    def restore(self):
        if self.client is None or self.original is None:
            return
        self.write(p13.RATED_MA)
        if self.client.get() != self.original:
            self.restore_check = "failed"
            raise Refused("final A61A differs from the original block")

    def last_resort_restore(self):
        """After any normal-restore exception, try the setup-time block carrying 99000.
        Verify A61A control readback only, not A619 firmware status, and report failure.
        A reboot re-seeds 99 A from the VBIOS."""
        try:
            self.event("last-resort restore: writing the setup-time block")
            status, _ = self.client.set(bytes(self.original))
            if status:
                raise Refused("E61B SET returned RM 0x%x" % status)
            if self.client.get() != self.original:
                raise Refused("readback differs from the setup-time block")
            self.dirty = False
            self.restore_check = "a61a-only"
            self.current = p13.RATED_MA
            self.event("last-resort restore: A61A readback matched; A619 not checked")
        except Exception as exc:
            self.restore_check = "failed"
            self.failures.append("LAST-RESORT RESTORATION FAILED: %s" % exc)
            print(
                "P13 MAY STILL BE RAISED: reboot to re-seed 99 A from the VBIOS.",
                file=sys.stderr,
                flush=True,
            )

    def event(self, text):
        line = dict(epoch=time.time(), elapsed=self.elapsed(), text=text)
        self.events.append(line)
        with (self.out / "events.jsonl").open("a") as f:
            f.write(json.dumps(line) + "\n")

    def elapsed(self):
        return None if self.begin is None else time.time() - self.begin

    # ---- guards (failures trigger cleanup)
    def guard(self):
        gpu = policy.rows(self.telemetry / "gpu.csv")
        platform = policy.rows(self.telemetry / "platform.csv")
        if gpu:
            # 85 C, slowdown/brake flags, enforced 125 W and the booted mode's P2 maximum.
            policy.check_gpu(gpu[-1], image_mode=self.image_mode)
        if platform and not policy.platform_ok(platform[-1]):
            raise Refused("AC or Turbo changed")
        if policy.has_xid(self.telemetry / "journal.log"):
            raise Refused("Xid in journal")
        if self.begin is not None and 20 <= self.elapsed() <= self.workload_s:
            policy.check_loaded(gpu, platform, time.time(), image_mode=self.image_mode)

    # ---- run
    def run(self):
        self.setup()
        self.telemetry.parent.mkdir(exist_ok=True)
        # The desktop witness wrapper launches bench.sh and captures X11 window geometry plus the
        # kscreen configuration at +30 s and +90 s; the runner refuses a non-panel placement.
        os.environ["PT516_WITNESS_SECONDS"] = str(self.seconds)
        bench = [str(PYTHON), str(POLICY / "witness.py")]
        args = [
            "bash",
            str(LAB / "collect.sh"),
            self.telemetry.name,
            "--runs-dir",
            str(self.out),
            "--pre",
            "10",
            "--post",
            "10",
            "--",
        ] + bench
        print(
            "Starting %d s panel FurMark (Vulkan 2560x1440). Ctrl-C attempts the 99 A restore and stops."
            % self.seconds,
            flush=True,
        )
        with (self.out / "collector.log").open("w") as log:
            self.collector = subprocess.Popen(
                args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            started = time.monotonic()
            done = 0
            next_snap = 20
            while self.collector.poll() is None:
                if time.monotonic() - started > self.seconds + 120:
                    raise Refused("collector deadline exceeded")
                if self.begin is None:
                    self.begin = policy.bench_epoch(self.telemetry / "events.log")
                    if self.begin is None and time.monotonic() - started > 90:
                        raise Refused("FurMark did not start")
                self.guard()
                if self.begin is not None:
                    el = self.elapsed()
                    self.check_placement(el)
                    while done < len(self.schedule) and el >= self.schedule[done][0]:
                        at, action, value = self.schedule[done]
                        if el > at + 4:
                            raise Refused(
                                "missed step window: %s %s at %d" % (action, value, at)
                            )
                        if action == "set":
                            self.write(value)
                        else:
                            self.snapshot(value, expect=self.current)
                        done += 1
                    if el >= next_snap and 20 <= el <= self.workload_s:
                        self.snapshot("cadence", expect=self.current)
                        next_snap = el + 4
                time.sleep(0.1)
            if self.collector.returncode or done != len(self.schedule):
                raise Refused(
                    "workload ended before the schedule completed (%d/%d)"
                    % (done, len(self.schedule))
                )
        if self.client.get() != self.original:
            raise Refused("final A61A differs from the original")
        meta = json.loads((self.telemetry / "metadata.json").read_text())
        if meta.get("child", {}).get("exit_status") != 0:
            raise Refused("workload failed")
        policy.check_witness_result(self.telemetry)
        self.geometry = validate_witness(self.telemetry)
        self.check_result()

    # ---- workload placement and renderer checks
    def check_placement(self, elapsed):
        """At +30 s the desktop witness has captured the FurMark window; it must be fullscreen on the
        panel (eDP) with the panel's logical bounds. Check any available renderer output."""
        if self.witness_checked or elapsed < 36:
            return
        path = self.telemetry / "witness/geometry-30s/result.json"
        if not path.exists():
            raise Refused(
                "desktop witness did not capture the window by +36 s; placement unverified"
            )
        witness = json.loads(path.read_text())
        panel_geometry(witness)  # raises Stop unless fullscreen on the unique eDP panel
        self.witness_checked = True
        self.event(
            "placement verified: fullscreen on %s"
            % [d["name"] for d in witness["displays"] if d["name"].startswith("eDP")]
        )
        self.check_renderer()

    def check_renderer(self, *, required=False):
        """Check the GPU name; the workload log may be unavailable until collection ends."""
        if self.renderer is None:
            self.renderer = self.furmark_result().get("renderer")
        if self.renderer is None:
            if required:
                raise Refused("FurMark renderer is missing from the completed run")
            return
        if "NVIDIA" not in self.renderer or "3080 Ti" not in self.renderer:
            raise Refused(
                "FurMark renderer is %r, not the RTX 3080 Ti; workload landed on the wrong GPU"
                % self.renderer
            )

    def check_result(self):
        """Require a renderer for every loaded run and a complete benchmark for a hold."""
        result = self.furmark_result()
        self.renderer = result.get("renderer")
        self.check_renderer(required=True)
        if self.hold_ma is not None:
            for key in ("score", "duration_ms"):
                value = result.get(key)
                if type(value) is not int or value <= 0:
                    raise Refused(f"FurMark {key} must be a positive integer")
            # Allow log timing overhead, but reject a truncated or different benchmark.
            if abs(result["duration_ms"] - self.seconds * 1000) > 1000:
                raise Refused(
                    "FurMark duration differs from the requested run by more than 1 s"
                )
        return result

    def furmark_result(self):
        """Parse FurMark's own log: renderer, SCORE, FPS min/avg/max, duration. Missing fields -> absent keys."""
        out = {}
        log = self.telemetry / "workload/furmark-furmark.log"
        if not log.exists():
            return out
        # Lines look like "(17:13:23)\t- SCORE                : 53771"; the timestamp has colons,
        # so split on the "<name> : <value>" separator after the leading "- ".
        for line in log.read_text(errors="replace").splitlines():
            body = line.split("\t", 1)[1] if "\t" in line else line
            if not body.startswith("- ") or " : " not in body:
                continue
            name, value = (x.strip() for x in body[2:].split(" : ", 1))
            try:
                if name == "renderer":
                    out["renderer"] = value
                elif name == "SCORE":
                    out["score"] = int(value)
                elif name == "FPS (min/avg/max)":
                    out["fps_min"], out["fps_avg"], out["fps_max"] = [
                        int(x) for x in value.split("/")
                    ]
                elif name == "duration":
                    duration, unit = value.split()
                    if unit == "ms":
                        out["duration_ms"] = int(duration)
            except ValueError:
                continue
        return out

    def smoke(self):
        """Smoke: exercise the transport at idle. The only write is the identity SET 99000 -> 99000."""
        self.is_smoke = True
        self.setup()
        before = self.client.get()
        if before != self.original:
            raise Refused("A61A changed between the original GET and the smoke SET")
        self.sets_issued += 1
        status, _ = self.client.set(p13.build_set(before, p13.RATED_MA, self.cap))
        self.restore_check = "failed"
        self.event("identity set 99000 rm 0x%x" % status)
        if status:
            raise Refused("identity E61B SET returned RM 0x%x" % status)
        if self.client.get() != self.original:
            raise Refused("A61A differs from the original after the identity SET")
        time.sleep(1.0)
        self.snapshot("after-identity-set", expect=p13.RATED_MA)
        self.restore_check = "a61a+a619"
        return dict(
            smoke=True,
            image_mode=self.image_mode,
            cap_ma=self.cap,
            identity_set_rm_status=status,
            p13=self.snapshots[-1]["p13"],
        )

    def cleanup(self):
        if self.collector:
            try:
                policy.stop_owned(self.collector)
            except Exception as exc:
                self.failures.append("workload cleanup: %s" % exc)
        if self.client and self.dirty:
            try:
                self.restore()
                self.event("restored after failure")
            except Exception as exc:
                self.failures.append("RESTORATION: %s" % exc)
                self.last_resort_restore()
        if self.client:
            try:
                self.client.close()
            except Exception as exc:
                self.failures.append("client close: %s" % exc)

    # ---- per-window clocks and whole-run score
    def analyze(self):
        if self.hold_ma is not None:
            return self.analyze_hold()
        windows = []
        current = p13.RATED_MA
        start = 20.0
        label = "ref"
        for at, action, value in self.schedule:
            if action != "set":
                continue
            windows.append((label, current, start, at))
            current = value
            start = at + 5
            label = (
                "pulse"
                if value == 95000
                else ("ref" if value == p13.RATED_MA else "up%d" % (value // 1000))
            )
        windows.append((label, current, start, self.workload_s))
        gpu = policy.rows(self.telemetry / "gpu.csv")
        rows = []
        for label, value, a, b in windows:
            if b - a < 8:
                continue
            g = [r for r in gpu if a <= float(r["host_epoch"]) - self.begin <= b]
            snaps = [
                s
                for s in self.snapshots
                if s["elapsed"] is not None and a <= s["elapsed"] <= b
            ]
            rows.append(
                dict(
                    label=label,
                    p13_set_ma=value,
                    start=a,
                    end=b,
                    sm_mhz_median=statistics.median(
                        float(r["clocks.current.sm [MHz]"]) for r in g
                    )
                    if g
                    else None,
                    power_w_mean=statistics.mean(
                        float(r["power.draw [W]"])
                        for r in g
                        if float(r["power.draw [W]"]) > 2
                    )
                    if g
                    else None,
                    temp_c_max=max(float(r["temperature.gpu"]) for r in g)
                    if g
                    else None,
                    p13_reading_ma=statistics.median(s["p13"]["reading"] for s in snaps)
                    if snaps
                    else None,
                    p13_output_ma=sorted({s["p13"]["gsp_type3_output"] for s in snaps}),
                    p2_reading_mw=statistics.median(
                        s["p2"]["reading_hint_raw"] for s in snaps
                    )
                    if snaps
                    else None,
                    header_match=sorted(
                        {
                            tuple(s["clock_comparison"]["exact_matches"][0])
                            for s in snaps
                        }
                    )
                    if snaps
                    else None,
                )
            )
        result = dict(windows=rows, verdicts=self.verdicts(rows))
        save(self.out / "analysis.json", result)
        return result

    def analyze_hold(self):
        """Hold mode: the whole-run FurMark score is the outcome (a frame count over the scored
        duration). SM/power/P13/P2 medians over the held interval are context."""
        fm = self.check_result()
        a, b = 10.0, self.workload_s - 10.0
        gpu = [
            r
            for r in policy.rows(self.telemetry / "gpu.csv")
            if a <= float(r["host_epoch"]) - self.begin <= b
        ]
        snaps = [
            s
            for s in self.snapshots
            if s["elapsed"] is not None and a <= s["elapsed"] <= b
        ]
        result = dict(
            hold_ma=self.hold_ma,
            score=fm.get("score"),
            duration_ms=fm.get("duration_ms"),
            score_per_second=(fm["score"] * 1000.0 / fm["duration_ms"])
            if fm.get("score") and fm.get("duration_ms")
            else None,
            fps_min_avg_max=[fm.get("fps_min"), fm.get("fps_avg"), fm.get("fps_max")],
            renderer=fm.get("renderer"),
            sm_mhz_median=statistics.median(
                float(r["clocks.current.sm [MHz]"]) for r in gpu
            )
            if gpu
            else None,
            power_w_mean=statistics.mean(
                float(r["power.draw [W]"])
                for r in gpu
                if float(r["power.draw [W]"]) > 2
            )
            if gpu
            else None,
            temp_c_max=max(float(r["temperature.gpu"]) for r in gpu) if gpu else None,
            p13_reading_ma=statistics.median(s["p13"]["reading"] for s in snaps)
            if snaps
            else None,
            p13_output_ma=sorted({s["p13"]["gsp_type3_output"] for s in snaps}),
            p2_reading_mw=statistics.median(s["p2"]["reading_hint_raw"] for s in snaps)
            if snaps
            else None,
            header_match=sorted(
                {tuple(s["clock_comparison"]["exact_matches"][0]) for s in snaps}
            )
            if snaps
            else None,
            geometry=getattr(self, "geometry", None),
        )
        save(self.out / "analysis.json", result)
        return result

    def verdicts(self, rows):
        by = {}
        for r in rows:
            by.setdefault(r["label"], []).append(r)

        def mean(label, key):
            v = [r[key] for r in by.get(label, []) if r.get(key) is not None]
            return statistics.mean(v) if v else None

        ref_sm = mean("ref", "sm_mhz_median")
        pulse_sm = mean("pulse", "sm_mhz_median")
        v = {
            "pulse_sm_pct": 100 * (pulse_sm - ref_sm) / ref_sm
            if pulse_sm and ref_sm
            else None
        }
        for label in sorted(label for label in by if label.startswith("up")):
            sm = mean(label, "sm_mhz_median")
            pw = mean(label, "power_w_mean")
            rd = mean(label, "p13_reading_ma")
            p2r = mean(label, "p2_reading_mw")
            hdr = sorted({h for r in by[label] for h in (r["header_match"] or [])})
            v[label] = dict(
                sm_pct=100 * (sm - ref_sm) / ref_sm if sm and ref_sm else None,
                power_w=pw,
                p13_reading_ma=rd,
                p2_reading_mw=p2r,
                header_matches=hdr,
                co_bound=bool(p2r and p2r >= 124000) or (hdr == [(0,)]),
            )
        return v

    def report(self, analysis):
        lines = [
            "P13 LADDER: " + ("INCOMPLETE" if self.failures else "COMPLETE")
        ] + self.failures
        if analysis and "hold_ma" in analysis:
            a = analysis
            lines.append(
                "HOLD %d mA: FurMark SCORE %s over %s ms (%.3f frames/s); FPS min/avg/max %s; renderer %s"
                % (
                    a["hold_ma"],
                    a["score"],
                    a["duration_ms"],
                    a["score_per_second"] or 0.0,
                    a["fps_min_avg_max"],
                    a["renderer"],
                )
            )
            lines.append(
                "context: SM median %s MHz, GPU %s W, P13 read %s A (out %s), P2 read %s W, header %s, max %s C"
                % (
                    "%.0f" % a["sm_mhz_median"] if a["sm_mhz_median"] else "-",
                    "%.1f" % a["power_w_mean"] if a["power_w_mean"] else "-",
                    "%.2f" % (a["p13_reading_ma"] / 1000)
                    if a["p13_reading_ma"]
                    else "-",
                    ",".join(str(o // 1000) for o in a["p13_output_ma"]),
                    "%.1f" % (a["p2_reading_mw"] / 1000) if a["p2_reading_mw"] else "-",
                    a["header_match"],
                    a["temp_c_max"],
                )
            )
            lines.append("P13 restore: %s" % (self.restore_check or "not attempted"))
            lines.append(
                "One arm of an ABBA; compare SCORE with its partner runs, not with history."
            )
            return "\n".join(lines) + "\n"
        if analysis and analysis.get("smoke"):
            lines.append(
                "SMOKE: image mode %d, cap %d mA, identity E61B SET rm_status=0x%x, P13 after: limit %d / out %d / inputs %r / table max %d"
                % (
                    analysis["image_mode"],
                    analysis["cap_ma"],
                    analysis["identity_set_rm_status"],
                    analysis["p13"]["limit"],
                    analysis["p13"]["gsp_type3_output"],
                    analysis["p13"]["gsp_type3_inputs"],
                    analysis["p13"]["table_max"],
                )
            )
            lines.append(
                "Transport exercised: GET, privileged SET and status readback all round-trip. No value other than 99000 was written."
            )
            return "\n".join(lines) + "\n"
        if analysis:
            lines.append(
                "window        set mA     SM MHz   GPU W   P13 read A  P13 out   P2 read W  header"
            )
            for r in analysis["windows"]:
                lines.append(
                    "%-12s %7d %8s %7s %10s %9s %10s  %s"
                    % (
                        r["label"],
                        r["p13_set_ma"],
                        "%.0f" % r["sm_mhz_median"] if r["sm_mhz_median"] else "-",
                        "%.1f" % r["power_w_mean"] if r["power_w_mean"] else "-",
                        "%.2f" % (r["p13_reading_ma"] / 1000)
                        if r["p13_reading_ma"]
                        else "-",
                        ",".join(str(o // 1000) for o in r["p13_output_ma"]),
                        "%.1f" % (r["p2_reading_mw"] / 1000)
                        if r["p2_reading_mw"]
                        else "-",
                        r["header_match"],
                    )
                )
            for k, val in analysis["verdicts"].items():
                lines.append(
                    "%s: %s"
                    % (k, json.dumps(val) if isinstance(val, (dict, float)) else val)
                )
        if not self.sets_issued:
            lines.append("P13 not touched: no E61B SET was issued.")
        else:
            lines.append("P13 restore: %s" % (self.restore_check or "not attempted"))
        lines.append(
            "Per-window SM clock and policy readings; the whole-run FurMark score is not attributed to a window. co_bound indicates P2 >= 124 W or a P0-only header match."
        )
        return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--plan", action="store_true", help="print the schedule; no hardware access"
    )
    ap.add_argument(
        "--cap-ma",
        type=int,
        default=None,
        help="sitting cap for P13 in mA (<= the booted image maximum); default: the image maximum (99000 on mode 3, 120000 on mode 4)",
    )
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument(
        "--hold",
        type=int,
        default=None,
        metavar="MA",
        help="hold P13 at MA for the whole run (one ABBA arm, scored by FurMark); use with --seconds 120",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="idle transport test: A618, A61A, A619 and one identity SET (99000 -> 99000); no workload",
    )
    args = ap.parse_args()
    if args.smoke and args.hold is not None:
        ap.error("--smoke and --hold are exclusive")
    if args.hold is not None and args.seconds < 120:
        ap.error("--hold requires at least 120 seconds for both placement checks")
    rungs = (104000, 109000, 114000)
    if args.plan and args.smoke:
        print(
            "Smoke: inventory, control and status reads; one identity SET (99000 -> 99000); no workload"
        )
        return 0
    if args.plan and args.hold is not None:
        steps, total = p13.hold_schedule(
            args.hold, max(args.hold, p13.RATED_MA), args.seconds
        )
        print(
            "hold %d mA for one %d s run: SET at +5 s, A619 every 20 s, restore at +%d s; outcome = FurMark SCORE"
            % (args.hold, args.seconds, args.seconds - 8)
        )
        return 0
    if args.plan:
        for mode in (3, 4):
            cap = min(args.cap_ma or p13.IMAGE_MAX_MA[mode], p13.IMAGE_MAX_MA[mode])
            steps, total = p13.ladder(cap, rungs)
            print(
                "image mode %d: cap %d mA, %d SETs, workload %d s (need --seconds >= %d)"
                % (mode, cap, sum(1 for s in steps if s[1] == "set"), total, total)
            )
            for at, action, value in steps:
                if action == "set":
                    print("  +%4ds  SET %d" % (at, value))
        print(
            "Each SET: live A61A GET -> +0x970 -> E61B -> A61A readback -> A619 verify. Restore 99000 under load."
        )
        return 0
    if os.geteuid() != 0 or os.environ.get("SUDO_USER") in (None, "root"):
        ap.error("run with sudo bash run.sh from your desktop terminal")
    account = pwd.getpwnam(os.environ["SUDO_USER"])
    os.umask(0o077)
    lock = open("/run/pt516-p13-ladder.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ap.error("another P13 ladder is active")
    out = Path(tempfile.mkdtemp(prefix="pt516-p13-ladder-", dir="/var/tmp"))
    # Root keeps the artifact root and its control files (rm-ops) until the end; the desktop
    # user only needs to traverse into the telemetry directory the collector hands over.
    os.chmod(out, 0o711)
    print("Artifacts: %s" % out, flush=True)
    sitting = Sitting(
        out,
        args.cap_ma,
        rungs,
        args.seconds,
        hold_ma=args.hold,
        smoke=args.smoke,
    )
    analysis = None

    def interrupted(signum, frame):
        raise Refused("interrupted by signal %d" % signum)

    for sig in SIGNALS:
        signal.signal(sig, interrupted)
    try:
        save(
            out / "tool-hashes.json",
            {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in list(HERE.glob("*.py"))
                + list(HERE.glob("*.gdb"))
                + list(HERE.glob("*.sh"))
                + [
                    POLICY / n
                    for n in (
                        "run.py",
                        "codec.py",
                        "series.py",
                        "witness.py",
                        "xclient_pid.py",
                    )
                ]
                + [p for p in (LAB / "lib").iterdir() if p.is_file()]
                + [
                    LAB / n
                    for n in ("collect.sh", "bench.sh", "policy-125/guard-boot.sh")
                ]
            },
        )
        if args.smoke:
            analysis = sitting.smoke()
        else:
            sitting.run()
    except (Exception, KeyboardInterrupt) as exc:
        sitting.failures.append(type(exc).__name__ + ": " + str(exc))
    finally:
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        sitting.cleanup()
        try:
            if sitting.begin is not None:
                analysis = sitting.analyze()
        except Exception as exc:
            sitting.failures.append("analysis: %s" % exc)
        text = sitting.report(analysis)
        save(out / "summary.txt", text.encode())
        save(
            out / "result.json",
            dict(
                complete=not sitting.failures,
                errors=sitting.failures,
                restored=None
                if not sitting.sets_issued
                else sitting.restore_check in ("a61a+a619", "a61a-only")
                and not sitting.dirty,
                restore_check=sitting.restore_check,
                sets_issued=sitting.sets_issued,
                renderer=sitting.renderer,
                placement_verified=sitting.witness_checked,
                ended_epoch=time.time(),
            ),
        )
        save(
            out / "MANIFEST.sha256",
            "".join(
                hashlib.sha256(p.read_bytes()).hexdigest()
                + "  "
                + str(p.relative_to(out))
                + "\n"
                for p in sorted(out.rglob("*"))
                if p.is_file() and not p.is_symlink() and p.name != "MANIFEST.sha256"
            ).encode(),
        )
        for base, dirs, files in os.walk(out):
            for name in dirs + files:
                os.chown(
                    Path(base) / name,
                    account.pw_uid,
                    account.pw_gid,
                    follow_symlinks=False,
                )
        lock.close()
        print(text + "Saved: %s" % out, flush=True)
    return 1 if sitting.failures else 0


if __name__ == "__main__":
    sys.exit(main())
