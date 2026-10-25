#!/usr/bin/env python3
"""Collect GPU telemetry, AC/Turbo state and the kernel/powerd journal at 1 Hz."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labcommon as lc  # noqa: E402

DESIRED_GPU_FIELDS = [
    "timestamp",
    "pstate",
    "utilization.gpu",
    "clocks.sm",
    "clocks.mem",
    "power.draw",
    "enforced.power.limit",
    "power.max_limit",
    "temperature.gpu",
    "clocks_event_reasons.active",
    "clocks_event_reasons.sw_power_cap",
    "clocks_event_reasons.hw_slowdown",
    "clocks_event_reasons.hw_thermal_slowdown",
    "clocks_event_reasons.hw_power_brake_slowdown",
    "clocks_event_reasons.sw_thermal_slowdown",
]


PLATFORM_COLUMNS = [
    "host_epoch",
    "time_local",
    "platform_profile",
    "ac_online",
    "broker_turbo",
]

TRACKED = ["platform_profile", "ac_online", "broker_turbo"]

KERNEL_EVENT_RE = re.compile(
    r"NVRM|Xid|nvidia|acer|thermal|throttl|Hardware Error|mce:|ACPI (BIOS )?Error|"
    r"ucsi|fallen off the bus|segfault|i915.*(hang|error)",
    re.I,
)
ERROR_RE = re.compile(r"Xid|fallen off the bus|NVRM.*(error|fail)|ERROR", re.I)


# ---------------------------------------------------------------- helpers


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return None


def read_int(path):
    t = read_text(path)
    try:
        return int(t) if t is not None else None
    except ValueError:
        return None


def which(name, *fallbacks):
    p = shutil.which(name)
    if p:
        return p
    for f in fallbacks:
        if os.access(f, os.X_OK):
            return f
    return None


def run_quiet(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1e12 else f"{v:.0f}"
    return str(v).replace(",", ";")


def csv_row(cols, row):
    return ",".join(fmt(row.get(c)) for c in cols) + "\n"


# ---------------------------------------------------------------- GPU fields


def help_query_fields(nvsmi):
    rc, out, _ = run_quiet([nvsmi, "--help-query-gpu"], timeout=30)
    names = set()
    for line in out.splitlines():
        if line.startswith('"'):
            names.update(re.findall(r'"([^"]+)"', line))
    return names


def validate_gpu_fields(nvsmi, desired=DESIRED_GPU_FIELDS, bus=None):
    """Keep only fields this driver knows, test the query, note N/A fields.

    A rejected field invalidates the whole query, so a failing combined query
    falls back to testing fields one at a time.
    """
    known = help_query_fields(nvsmi)
    used, unknown = [], []
    for f in desired:
        if f in known:
            used.append(f)
        else:
            unknown.append(f)
    sel = ["-i", bus] if bus else []

    def query(fields):
        return run_quiet(
            [
                nvsmi,
                *sel,
                "--query-gpu=" + ",".join(fields),
                "--format=csv,noheader,nounits",
            ],
            timeout=30,
        )

    rejected = []
    rc, out, err = query(used)
    if rc != 0 or "not a valid field" in (out + err):
        good = []
        for f in used:
            r1, o1, e1 = query([f])
            if r1 == 0 and "not a valid field" not in (o1 + e1):
                good.append(f)
            else:
                rejected.append(f)
        used = good
        rc, out, err = query(used)
    values = {}
    if rc == 0 and out.strip():
        cells = [c.strip() for c in out.strip().splitlines()[0].split(",")]
        if len(cells) == len(used):
            values = dict(zip(used, cells))
    na = [f for f, v in values.items() if lc.parse_value(v) is None]
    return {
        "requested": list(desired),
        "used": used,
        "unknown_to_driver": unknown,
        "rejected_by_query": rejected,
        "na_at_start": na,
        "values_at_start": values,
        "query_rc": rc,
        "query_error": err.strip()[:500],
    }


class Platform:
    def __init__(self):
        self.ac = [
            p
            for p in sorted(Path("/sys/class/power_supply").glob("*"))
            if read_text(p / "type") == "Mains"
        ]
        self.broker_state = Path("/run/turbo-broker/state.json")

    def platform_sample(self, now):
        on = [read_int(p / "online") for p in self.ac]
        state = lc.load_json(self.broker_state)
        observed = (state.get("observed") or {}) if isinstance(state, dict) else {}
        return dict(
            host_epoch=now,
            time_local=lc.iso_local(now),
            platform_profile=read_text("/sys/firmware/acpi/platform_profile"),
            ac_online=(int(any(on)) if on and None not in on else None),
            broker_turbo=observed.get("turbo"),
        )


class LineReader:
    def __init__(self, proc, name):
        self.proc, self.name, self.buf = proc, name, b""

    def read_lines(self):
        try:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return [], False
        except OSError:
            chunk = b""
        if not chunk:
            rest = self.buf.decode(errors="replace")
            self.buf = b""
            return ([rest] if rest.strip() else []), True
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        return [x.decode(errors="replace").rstrip("\r") for x in lines], False


class Sampler:
    def __init__(self, args):
        self.args = args
        self.rd = Path(args.run_dir)
        snaps = self.rd / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        self.stop = False
        self.failed = False
        self.nvsmi = which("nvidia-smi", "/run/current-system/sw/bin/nvidia-smi")
        self.journalctl = which("journalctl", "/run/current-system/sw/bin/journalctl")
        self.events = open(self.rd / "events.log", "a", buffering=1)
        self.journal_log = open(self.rd / "journal.log", "a", buffering=1)
        self.gpu_out = open(self.rd / "gpu.csv", "a", buffering=1)
        self.plat_out = open(self.rd / "platform.csv", "a", buffering=1)
        self.plat_out.write(",".join(PLATFORM_COLUMNS) + "\n")
        self.sel = selectors.DefaultSelector()
        self.children = {}
        self.gpu_header = None
        self.gpu_rows = 0
        self.nvsmi_restarts = 0
        self.nvsmi_restart_at = None
        self.last_limit = None
        self.prev_state = None
        self.dedupe = {}
        self.ready = False

    # -- logging
    def event(self, source, msg, epoch=None):
        self.events.write(
            lc.event_line(time.time() if epoch is None else epoch, source, msg)
        )

    def event_dedupe(self, source, msg, epoch, window=60.0):
        key = (source, re.sub(r"\d+", "#", msg)[:200])
        last = self.dedupe.get(key)
        if last and epoch - last[0] < window:
            self.dedupe[key] = (last[0], last[1] + 1)
            return
        if last and last[1]:
            self.event(
                source, f"(previous message repeated {last[1]} more times)", epoch
            )
        self.dedupe[key] = (epoch, 0)
        self.event(source, msg, epoch)

    # -- children
    def spawn(self, name, cmd, stderr_name):
        try:
            err = open(self.rd / "snapshots" / stderr_name, "ab")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL
            )
        except OSError as exc:
            self.event("sampler", f"could not start {name}: {exc}")
            return None
        os.set_blocking(proc.stdout.fileno(), False)
        reader = LineReader(proc, name)
        self.children[name] = (proc, reader)
        self.sel.register(proc.stdout, selectors.EVENT_READ, name)
        self.event("sampler", f"started {name} pid={proc.pid}: {' '.join(cmd)[:400]}")
        return proc

    def stop_child(self, name):
        proc, reader = self.children.pop(name, (None, None))
        if not proc:
            return
        try:
            self.sel.unregister(proc.stdout)
        except (KeyError, ValueError):
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.set_blocking(proc.stdout.fileno(), True)
        rest = proc.stdout.read() or b""
        if name == "nvidia-smi" and rest:
            for line in rest.decode(errors="replace").splitlines():
                self.on_gpu_line(line, time.time())

    def start_nvsmi(self):
        if not self.nvsmi:
            self.event("sampler", "nvidia-smi not found; gpu.csv stays empty")
            return
        sel = ["-i", self.args.gpu_bus] if self.args.gpu_bus else []
        cmd = [
            self.nvsmi,
            *sel,
            "--query-gpu=" + self.args.gpu_fields,
            "--format=csv,nounits",
            "-l",
            "1",
        ]
        self.spawn("nvidia-smi", cmd, "nvidia-smi-loop.stderr")

    def start_journal(self):
        if not self.journalctl:
            raise RuntimeError("journalctl unavailable; Xid monitoring is required")
        matches = [
            "_SYSTEMD_UNIT=nvidia-powerd.service",
            "UNIT=nvidia-powerd.service",
            "_TRANSPORT=kernel",
            "_SYSTEMD_UNIT=turbo-broker.service",
            "UNIT=turbo-broker.service",
        ]
        cmd = [self.journalctl, "-f", "-n", "0", "-o", "json", "--no-pager"]
        for i, match in enumerate(matches):
            if i:
                cmd.append("+")
            cmd.append(match)
        if self.spawn("journalctl", cmd, "journalctl.stderr") is None:
            raise RuntimeError("could not start required journal monitoring")

    # -- line handlers
    def on_gpu_line(self, line, now):
        if not line.strip():
            return
        if line.startswith("timestamp"):
            if self.gpu_header is None:
                self.gpu_header = [lc.norm_gpu_header(h) for h in line.split(",")]
                self.gpu_out.write("host_epoch, " + line + "\n")
            elif [lc.norm_gpu_header(h) for h in line.split(",")] != self.gpu_header:
                self.event(
                    "sampler", "nvidia-smi header changed after restart: " + line[:300]
                )
            return
        if line[:4].isdigit() and self.gpu_header:
            self.gpu_out.write(f"{now:.3f}, {line}\n")
            self.gpu_rows += 1
            cells = [c.strip() for c in line.split(",")]
            if len(cells) == len(self.gpu_header):
                row = dict(zip(self.gpu_header, cells))
                lim = lc.parse_value(row.get("enforced.power.limit"))
                # Dynamic Boost moves the limit with GPU activity (80 W at P8,
                # 93-105 W at P5/P2/P0 in balanced, inspected 2026-10-17), so
                # only steps of 2 W or more become events; gpu.csv has every value.
                if lc.is_num(lim) and (
                    self.last_limit is None or abs(lim - self.last_limit) >= 2.0
                ):
                    if self.last_limit is not None:
                        self.event(
                            "gpu",
                            f"enforced.power.limit {self.last_limit:g} -> {lim:g} W",
                            now,
                        )
                    self.last_limit = lim
            return
        self.event_dedupe("nvidia-smi", line.strip()[:300], now)

    def on_journal_line(self, line, now):
        try:
            e = json.loads(line)
        except ValueError:
            return
        msg = e.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode(
                errors="replace"
            )
        msg = " ".join(str(msg or "").split())
        try:
            ep = int(e.get("__REALTIME_TIMESTAMP")) / 1e6
        except (TypeError, ValueError):
            ep = now
        unit = (
            (
                e.get("_SYSTEMD_UNIT")
                if e.get("_SYSTEMD_UNIT") not in (None, "init.scope")
                else None
            )
            or e.get("UNIT")
            or e.get("USER_UNIT")
            or e.get("_SYSTEMD_USER_UNIT")
        )
        if e.get("_TRANSPORT") == "kernel":
            unit = "kernel"
        unit = unit or e.get("SYSLOG_IDENTIFIER") or "journal"
        if e.get("USER_UNIT") and e.get("_SYSTEMD_UNIT", "").startswith("user@"):
            unit = e["USER_UNIT"]
        self.journal_log.write(lc.event_line(ep, unit, msg))
        if unit == "kernel" and not KERNEL_EVENT_RE.search(msg):
            return
        src = (
            "journal-error"
            if ERROR_RE.search(msg) and (unit == "kernel" or "powerd" in unit)
            else "journal"
        )
        self.event_dedupe(src, f"{unit}: {msg[:400]}", ep)

    def handle(self, name):
        proc, reader = self.children.get(name, (None, None))
        if not reader:
            return
        lines, eof = reader.read_lines()
        now = time.time()
        for line in lines:
            if name == "nvidia-smi":
                self.on_gpu_line(line, now)
            elif name == "journalctl":
                self.on_journal_line(line, now)
        if eof:
            try:
                self.sel.unregister(proc.stdout)
            except (KeyError, ValueError):
                pass
            proc.wait()
            self.children.pop(name, None)
            self.event("sampler", f"{name} exited rc={proc.returncode}")
            if name == "journalctl" and not self.stop:
                self.failed = self.stop = True
                self.event("sampler", "journal monitoring stopped; stopping collection")
            if name == "nvidia-smi" and not self.stop:
                self.nvsmi_restarts += 1
                if self.nvsmi_restarts <= 5:
                    self.nvsmi_restart_at = time.monotonic() + 5 * self.nvsmi_restarts
                else:
                    self.event(
                        "sampler", "nvidia-smi exited too often; GPU logging stopped"
                    )

    # -- tick

    def tick(self, plat):
        now = time.time()
        row = plat.platform_sample(now)
        self.plat_out.write(csv_row(PLATFORM_COLUMNS, row))
        state = {k: row.get(k) for k in TRACKED}
        if self.prev_state is None:
            self.event(
                "state",
                "initial " + " ".join(f"{k}={fmt(v)}" for k, v in state.items()),
                now,
            )
        else:
            for k, v in state.items():
                if v != self.prev_state.get(k):
                    self.event(
                        "state", f"{k} {fmt(self.prev_state.get(k))} -> {fmt(v)}", now
                    )
        self.prev_state = state
        if not self.ready and self.gpu_rows > 0:
            self.ready = True
            (self.rd / ".sampler-ready").write_text(f"{now:.3f}\n")
            self.event("sampler", "ready (first GPU and platform rows written)")

    def resolve_gpu(self):
        """Pick the GPU bus id and validate the field list against this driver."""
        if not self.nvsmi:
            raise RuntimeError("nvidia-smi unavailable")
        if not self.args.gpu_bus:
            rc, out, _ = run_quiet(
                [self.nvsmi, "--query-gpu=pci.bus_id", "--format=csv,noheader"]
            )
            buses = (
                [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
            )
            if buses:
                self.args.gpu_bus = buses[0]
                if len(buses) > 1:
                    self.event(
                        "sampler", f"several NVIDIA GPUs {buses}; logging {buses[0]}"
                    )
        if not self.args.gpu_fields:
            v = validate_gpu_fields(self.nvsmi, bus=self.args.gpu_bus)
            v["gpu_bus"] = self.args.gpu_bus
            (self.rd / "gpu_fields.json").write_text(json.dumps(v, indent=2) + "\n")
            self.args.gpu_fields = ",".join(v["used"])
            if v["unknown_to_driver"] or v["rejected_by_query"]:
                self.event(
                    "sampler",
                    f"GPU fields dropped: unknown={v['unknown_to_driver']}"
                    f" rejected={v['rejected_by_query']}",
                )
            if v["na_at_start"]:
                self.event(
                    "sampler",
                    f"GPU fields N/A at start (kept, logged empty): {v['na_at_start']}",
                )

    def run(self):
        self.resolve_gpu()
        plat = Platform()
        self.event(
            "sampler",
            f"start pid={os.getpid()} euid={os.geteuid()} interval=1s "
            f"{lc.HARNESS_VERSION}",
        )
        if not self.args.gpu_fields:
            raise RuntimeError("no GPU fields available")
        self.start_journal()
        self.start_nvsmi()
        next_t = time.monotonic()
        while not self.stop:
            timeout = max(0.0, next_t - time.monotonic())
            for key, _ in self.sel.select(timeout):
                self.handle(key.data)
            mono = time.monotonic()
            if (
                self.nvsmi_restart_at
                and mono >= self.nvsmi_restart_at
                and not self.stop
            ):
                self.nvsmi_restart_at = None
                self.start_nvsmi()
            if mono >= next_t:
                try:
                    self.tick(plat)
                except Exception as exc:  # keep sampling; report the problem
                    self.event_dedupe(
                        "sampler",
                        f"tick error: {type(exc).__name__}: {exc}",
                        time.time(),
                    )
                next_t += 1.0
                if next_t < mono:
                    self.event(
                        "sampler",
                        f"fell behind by {mono - next_t:.1f}s; skipping ahead",
                    )
                    next_t = mono + 1.0
        (self.rd / ".sampler-ready").unlink(missing_ok=True)
        for name in list(self.children):
            self.stop_child(name)
        now = time.time()
        for (src, _), (first, count) in self.dedupe.items():
            if count:
                self.event(
                    src,
                    f"(a message repeated {count} more times since {lc.iso_local(first)})",
                    now,
                )
        self.event(
            "sampler",
            f"stop gpu_rows={self.gpu_rows} nvidia-smi restarts={self.nvsmi_restarts}",
        )
        for f in (
            self.events,
            self.journal_log,
            self.gpu_out,
            self.plat_out,
        ):
            if f is not None:
                f.close()
        self.sel.close()
        return 1 if self.failed else 0


def cmd_run(args):
    s = Sampler(args)

    def on_term(signum, frame):
        s.stop = True

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # collect.sh decides when to stop
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)
    return s.run()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--run-dir", required=True)
    r.add_argument(
        "--gpu-fields", help="comma list; default: validate DESIRED_GPU_FIELDS"
    )
    r.add_argument("--gpu-bus", default="0000:01:00.0")
    return cmd_run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
