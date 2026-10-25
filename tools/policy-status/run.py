#!/usr/bin/env python3
"""Bounded idle policy/seed capture and shared experiment guards. Importing this module does nothing."""

import argparse
import shutil
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import tempfile
import time

from codec import EXPECTED, IMAGE_MAXIMA, inventory, status

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
GDB = Path(shutil.which("gdb") or "gdb")
PYTHON = Path(sys.executable)


class Stop(RuntimeError):
    pass


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def process_table():
    rows = {}
    for p in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = p.read_text()
            cols = text[text.rfind(")") + 2 :].split()
            rows[int(p.parent.name)] = (int(cols[1]), cols[19])
        except (OSError, ValueError, IndexError):
            continue
    return rows


def stop_owned(proc):
    """Stop only this launched process and its recorded descendants, guarding PID reuse."""
    if proc is None or proc.poll() is not None:
        return
    table = process_table()
    selected = {proc.pid}
    while True:
        more = {pid for pid, (parent, _) in table.items() if parent in selected}
        if more <= selected:
            break
        selected |= more
    identities = {pid: table[pid][1] for pid in selected if pid in table}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        current = process_table()
        for pid, start in identities.items():
            if current.get(pid, (None, None))[1] == start:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        if sig == signal.SIGTERM:
            time.sleep(2)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise Stop("launched process did not exit; partial artifacts preserved")


def command(args, timeout=10, env=None):
    proc = subprocess.Popen(
        [str(x) for x in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output
    except BaseException:
        stop_owned(proc)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def rows(path):
    try:
        with path.open() as f:
            return [
                r
                for r in csv.DictReader(f, skipinitialspace=True)
                if r and None not in r and all(v is not None for v in r.values())
            ]
    except FileNotFoundError:
        return []


def expected_limits(image_mode):
    """(enforced W, max W) NVML must report for a boot-image mode: the runtime target is
    125 W in every mode; the maximum is the image's policy-2 table maximum."""
    if image_mode not in IMAGE_MAXIMA:
        raise Stop("unknown image mode %r" % (image_mode,))
    return 125.0, IMAGE_MAXIMA[image_mode][0] / 1000.0


def power_preflight(out=None):
    """AC must be connected. Turbo is required only where the Turbo broker (acer-pt516-nix)
    is installed; without it, AC is read from sysfs and no platform profile is enforced."""
    broker = shutil.which("turbo-broker")
    if broker:
        rc, text = command([broker, "status", "--json"])
        if out is not None:
            (out / "turbo-preflight.json").write_text(text)
        if rc:
            raise Stop("Turbo status query failed")
        observed = json.loads(text).get("broker", {}).get("observed", {})
        if observed.get("turbo") is not True or observed.get("onBattery") is not False:
            raise Stop("enable Turbo and connect AC before this diagnostic")
        return {"turbo_broker": True}
    online = []
    for t in Path("/sys/class/power_supply").glob("*/type"):
        if t.read_text().strip() == "Mains" and t.with_name("online").exists():
            online.append(t.with_name("online").read_text().strip())
    if "1" not in online:
        raise Stop("connect AC before this diagnostic")
    return {"turbo_broker": False}


def platform_ok(p):
    """Telemetry guard: AC stays connected, and Turbo stays on where the broker reports it."""
    if p["ac_online"] not in ("1", "1.0"):
        return False
    turbo = p.get("broker_turbo")
    if turbo in (None, "", "None"):
        return True
    return turbo in ("1", "True", "true")


def check_gpu(row, idle=False, image_mode=3):
    def value(key):
        v = float(row[key])
        if not math.isfinite(v):
            raise Stop("nonfinite GPU telemetry")
        return v

    enforced, maximum = expected_limits(image_mode)
    if (
        value("enforced.power.limit [W]") != enforced
        or value("power.max_limit [W]") != maximum
    ):
        raise Stop(
            "mode %d runtime cap %g W / maximum %g W is not active (read %g / %g); no limit or service was changed"
            % (
                image_mode,
                enforced,
                maximum,
                value("enforced.power.limit [W]"),
                value("power.max_limit [W]"),
            )
        )
    if value("temperature.gpu") >= 85:
        raise Stop("GPU temperature reached diagnostic stop threshold 85 C")
    if idle and value("utilization.gpu [%]") > 20:
        raise Stop("GPU is busy; close other games/benchmarks before the idle capture")
    for key in [
        "hw_slowdown",
        "hw_thermal_slowdown",
        "hw_power_brake_slowdown",
        "sw_thermal_slowdown",
    ]:
        if row.get("clocks_event_reasons." + key) == "Active":
            raise Stop("limiting condition appeared: " + key)


def check_loaded(gpu, platform, now, image_mode=3):
    if len(gpu) < 5 or now - float(gpu[-1]["host_epoch"]) > 4:
        raise Stop("GPU sampler stale or incomplete")
    last = gpu[-5:]
    for r in last:
        check_gpu(r, image_mode=image_mode)
    if any(float(r["utilization.gpu [%]"]) < 90 for r in last):
        raise Stop(
            "no sustained GPU load during monitored interval; no automatic retry"
        )
    if not platform or now - float(platform[-1]["host_epoch"]) > 4:
        raise Stop("platform sampler stale or incomplete")
    p = platform[-1]
    if not platform_ok(p):
        raise Stop("AC or Turbo is no longer active")
    return {"gpu_last_five": last, "platform_last": p, "checked_at": now}


def check_witness_result(telemetry):
    path = telemetry / "witness/result.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    try:
        result = json.loads(text)
        complete = result["complete"]
        if complete is not True and complete is not False:
            raise ValueError("complete is not a boolean")
    except (ValueError, KeyError, TypeError) as exc:
        raise Stop(
            "desktop witness result invalid; partial artifacts retained"
        ) from exc
    if not complete:
        raise Stop(
            "desktop witness failed: " + str(result.get("error", "unspecified failure"))
        )


def bench_epoch(path):
    try:
        for line in path.read_text().splitlines():
            parts = line.split("\t", 3)
            if (
                len(parts) == 4
                and parts[2] == "bench"
                and parts[3].startswith("bench-start name=furmark ")
            ):
                return float(parts[1])
    except FileNotFoundError:
        pass
    return None


class Sitting:
    def __init__(self, out):
        self.out = out
        self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.collector = None
        self.samples = []
        self.image_mode = None

    def preflight(self):
        env = dict(os.environ, PT516_ALLOW_MODE4="1")
        rc, text = command(
            [
                "bash",
                "-c",
                'source "$1" && echo "policy_mode=$policy_mode"',
                "_",
                str(LAB / "policy-125/guard-boot.sh"),
            ],
            env=env,
        )
        (self.out / "boot-guard.txt").write_text(text)
        if rc:
            raise Stop("boot identity check failed: " + text.strip())
        self.image_mode = int(text.strip().rsplit("policy_mode=", 1)[1].split()[0])
        library = Path("/run/opengl-driver/lib/libnvidia-ml.so.1")
        if sha(library) != EXPECTED:
            raise Stop("NVML hash differs")
        for p in [GDB, PYTHON]:
            if not os.access(p, os.X_OK):
                raise Stop("prepared executable unavailable: " + str(p))
        rc, text = command(["systemctl", "is-active", "nvidia-powerd"])
        if rc or text.strip() != "active":
            raise Stop("nvidia-powerd must already be active; no automatic restart")
        dump(
            self.out / "identity.json",
            dict(
                boot_id=self.boot,
                library_sha256=EXPECTED,
                image_mode=self.image_mode,
                planned_gets=2,
                idle_only=True,
                limits_changed=False,
            ),
        )
        return

    def snapshot(self, label):
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != self.boot:
            raise Stop("boot identity changed")
        folder = self.out / "policies" / label
        folder.mkdir(parents=True)
        print("Capturing policy snapshot: " + label, flush=True)
        queries = [("info", "a618"), ("status", "a619")]
        for query, name in queries:
            dest = folder / query
            dest.mkdir()
            env = dict(
                os.environ,
                PT516_POLICY_DIR=str(dest),
                PT516_POLICY_LAB=str(HERE),
                PT516_POLICY_QUERY=query,
                PYTHONDONTWRITEBYTECODE="1",
            )
            for key in ["LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH"]:
                env.pop(key, None)
            args = [
                GDB,
                "--nx",
                "--batch",
                "-x",
                HERE / "capture.gdb",
                "--args",
                PYTHON,
                HERE / "bootstrap.py",
            ]
            event(self.out, label + "/" + query + " launch")
            try:
                rc, text = command(args, timeout=12, env=env)
            except subprocess.TimeoutExpired as exc:
                text = exc.output or b""
                (dest / "trace.log").write_text(
                    text.decode(errors="replace") if isinstance(text, bytes) else text
                )
                raise Stop(query + " timed out; no retry") from exc
            finally:
                event(self.out, label + "/" + query + " process-exit")
            (dest / "trace.log").write_text(text)
            if rc or "POLICY capture complete;" not in text or "POLICY STOP:" in text:
                reasons = [s for s in text.splitlines() if "STOP:" in s]
                raise Stop(
                    label
                    + "/"
                    + query
                    + ": "
                    + ("; ".join(reasons) or "incomplete GDB capture")
                )
            record = json.loads((dest / (name + ".json")).read_text())
            raw = (dest / (name + ".bin")).read_bytes()
            if (
                record["rm_callback_status"] != "0x0"
                or record["callback_count"] != 1
                or not record["buffer_guards_intact"]
                or not record["original_a618_buffer_unchanged"]
                or record["reply_sha256"] != hashlib.sha256(raw).hexdigest()
            ):
                raise Stop("capture metadata/guards failed")
            if query == "info":
                inv = inventory(raw)
                if inv["image_mode"] != self.image_mode:
                    raise Stop(
                        "A618 inventory is a mode %d image but the host booted mode %r"
                        % (inv["image_mode"], self.image_mode)
                    )
            else:
                decoded = status(raw, (folder / "info/a618.bin").read_bytes())
                self.samples.append(
                    {"label": label, "capture": record, "decoded": decoded}
                )
                dump(self.out / "policy-snapshots.json", self.samples)
        dump(folder / "snapshot.json", self.samples[-1])

    def seed_verdict(self):
        """Envelope acceptance: with widened table maxima the boot seeds must still be the rated values.
        Reads only the saved idle snapshot; no further GPU access."""
        rec = self.samples[-1]["decoded"]
        p2 = next(p for p in rec["policies"] if p["index"] == 2)
        p13 = next(p for p in rec["policies"] if p["index"] == 13)
        t3 = p13["arbiters"]["gsp_type3"]
        pmu = p13["arbiters"]["pmu"]
        checks = {
            "image_mode": self.image_mode,
            "p2_table_min_rated_max_mW": [p2["minimum"], p2["rated"], p2["maximum"]],
            "p13_table_min_rated_max_mA": [
                p13["minimum"],
                p13["rated"],
                p13["maximum"],
            ],
            "p13_limit_hint_mA": p13["limit_hint_raw"],
            "p13_gsp_type3_output_mA": p13["gsp_type3_output_raw"],
            "p13_gsp_type3_default_mA": t3["default_raw"],
            "p13_gsp_type3_inputs": t3["inputs"],
            "p13_pmu_output_mA": p13["pmu_arbiter_output_raw"],
            "p13_pmu_input_count": pmu["count"],
            "p2_gsp_type3_output_mW": p2["gsp_type3_output_raw"],
            "p2_pmu_output_mW": p2["pmu_arbiter_output_raw"],
            "p2_gsp_type3_inputs": p2["arbiters"]["gsp_type3"]["inputs"],
        }
        seed_ok = (
            p13["limit_hint_raw"] == 99000
            and p13["gsp_type3_output_raw"] == 99000
            and t3["default_raw"] == 99000
            and p13["pmu_arbiter_output_raw"] == 99000
            and t3["count"] == 1
            and t3["inputs"][0]["client_raw"] == 0xFE
            and t3["inputs"][0]["value_raw"] == 99000
        )
        p2_ok = (
            p2["gsp_type3_output_raw"] == 125000
            and p2["pmu_arbiter_output_raw"] == 125000
        )
        checks["p13_seed_is_rated_99000"] = seed_ok
        checks["p2_runtime_is_125000"] = p2_ok
        if self.image_mode == 4:
            checks["verdict"] = (
                (
                    "PASS: envelope maxima present (P2 %d mW, P13 %d mA) and inert at idle; runtime P13 99 A, P2 125 W"
                    % (p2["maximum"], p13["maximum"])
                )
                if seed_ok and p2_ok
                else "FAIL: seed or runtime differs from stock; do NOT raise anything this boot"
            )
        else:
            checks["verdict"] = "mode 3 reference: " + (
                "stock seeds confirmed"
                if seed_ok and p2_ok
                else "unexpected idle state"
            )
        dump(self.out / "seed-check.json", checks)
        return checks

    def run(self):
        self.preflight()
        self.snapshot("idle")
        return self.seed_verdict()


def summary(samples, analysis, failure):
    lines = ["POLICY STATUS DIAGNOSTIC: " + ("INCOMPLETE" if failure else "COMPLETE")]
    if failure:
        lines.append("Reason: " + failure)
    if analysis and "verdict" in analysis:
        lines.append("SEED CHECK: " + analysis["verdict"])
        lines.append(
            "P2 table %r mW; P13 table %r mA; P13 type-3 inputs %r"
            % (
                analysis["p2_table_min_rated_max_mW"],
                analysis["p13_table_min_rated_max_mA"],
                analysis["p13_gsp_type3_inputs"],
            )
        )
    lines += [
        "Raw policy values (mA or mW; limit/value labels are source-derived hints):",
        "snapshot      policy unit       reading       limit       GSP-3     PMU-output",
    ]
    for sample in samples:
        for r in sample["decoded"]["policies"]:
            if r["index"] in (0, 2, 12, 13, 14, 15, 16, 18):
                lines.append(
                    "%-13s %5d %-3s %13d %11d %11d %14d"
                    % (
                        sample["label"],
                        r["index"],
                        r["unit"],
                        r["reading_hint_raw"],
                        r["limit_hint_raw"],
                        r["gsp_type3_output_raw"],
                        r["pmu_arbiter_output_raw"],
                    )
                )
    lines.append(
        "Snapshots do not by themselves establish freshness or the unique winning limiter."
    )
    lines.append("No power limit, service, profile or display setting was changed.")
    return "\n".join(lines) + "\n"


def event(out, message):
    now = time.time()
    with (out / "diag-events.log").open("a") as f:
        f.write(
            "%s\t%.6f\tdiag\t%s\n"
            % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), now, message)
        )


def has_xid(path):
    import re

    try:
        return bool(re.search(r"NVRM:.*\bXid\b", path.read_text()))
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Read the idle policy inventory and verify the boot seeds. No settings changed."
    )
    parser.add_argument(
        "--plan", action="store_true", help="print the plan without hardware access"
    )
    parser.add_argument(
        "--idle-only",
        action="store_true",
        help="idle seed check (the only capture mode)",
    )
    args = parser.parse_args()
    if args.plan:
        print(
            "Envelope seed check: pinned mode 3 or 4; nvidia-powerd active.\n"
            "One A618 GET and one A619 GET; check the 99 A seed and 125 W target.\n"
            "No workload or SET. No hardware accessed by --plan."
        )
        return 0
    if (
        os.geteuid() != 0
        or not os.environ.get("SUDO_USER")
        or os.environ["SUDO_USER"] == "root"
    ):
        parser.error("run with sudo bash run.sh from your desktop terminal")
    account = pwd.getpwnam(os.environ["SUDO_USER"])
    os.umask(0o077)
    out = Path(tempfile.mkdtemp(prefix="pt516-policy-status-", dir="/var/tmp"))
    os.chmod(
        out, 0o711
    )  # root-owned until the final handover; the collector's run directory is handed to the user
    sitting = None
    failure = None
    analysis = None
    print("Artifacts: " + str(out), flush=True)

    def interrupted(signum, frame):
        raise Stop("interrupted by signal %d" % signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        source_files = [
            p for p in HERE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
        ]
        source_files += [
            LAB / "policy-125/guard-boot.sh",
        ]
        dump(out / "tool-hashes.json", {str(p): sha(p) for p in source_files})
        sitting = Sitting(out)
        analysis = sitting.run()
    except (Exception, KeyboardInterrupt) as exc:
        failure = type(exc).__name__ + ": " + str(exc)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        if sitting:
            try:
                stop_owned(sitting.collector)
            except Exception as exc:
                failure = (failure + "; " if failure else "") + "cleanup: " + str(exc)
        text = summary(sitting.samples if sitting else [], analysis, failure)
        (out / "summary.txt").write_text(text)
        dump(
            out / "result.json",
            dict(complete=failure is None, error=failure, ended_epoch=time.time()),
        )
        (out / "MANIFEST.sha256").write_text(
            "".join(
                sha(p) + "  " + str(p.relative_to(out)) + "\n"
                for p in sorted(out.rglob("*"))
                if p.is_file() and not p.is_symlink() and p.name != "MANIFEST.sha256"
            )
        )
        for base, dirs, files in os.walk(out, followlinks=False):
            for name in dirs + files:
                os.chown(
                    Path(base) / name,
                    account.pw_uid,
                    account.pw_gid,
                    follow_symlinks=False,
                )
        print(text, flush=True)
        print("Saved: " + str(out), flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())
