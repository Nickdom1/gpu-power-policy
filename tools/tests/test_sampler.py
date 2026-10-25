"""Collector I/O contracts with fake commands and platform state; no GPU or journal access."""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / "lib"


class SamplerIO(unittest.TestCase):
    def run_fixture(self, journal_exits=False):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        for name, body in {
            "nvidia-smi": """
from pathlib import Path
import sys, time
if '--help-query-gpu' in sys.argv:
    print('"timestamp"\\n"power.draw"')
elif '-l' not in sys.argv:
    print('2026/10/24 12:00:00, 125')
else:
    flag = Path(__file__).with_name('restarted')
    first = not flag.exists()
    flag.touch()
    print('timestamp, power.draw [W]', flush=True)
    print('2026/10/24 12:00:00, 125', flush=True)
    if not first:
        time.sleep(30)
""",
            "journalctl": """
import json, time
print(json.dumps({'_TRANSPORT': 'kernel', 'MESSAGE': 'NVRM: Xid 79 fixture'}), flush=True)
"""
            + ("" if journal_exits else "time.sleep(30)\n"),
        }.items():
            path = root / name
            path.write_text("#!" + sys.executable + "\n" + body)
            path.chmod(0o700)
        script = """
import sampler, types, sys
class Platform:
    def platform_sample(self, now):
        return dict(host_epoch=now, time_local='', platform_profile='performance', ac_online=1, broker_turbo=True)
sampler.Platform = Platform
sys.exit(sampler.cmd_run(types.SimpleNamespace(run_dir=sys.argv[1], gpu_fields='timestamp,power.draw', gpu_bus='0000:01:00.0')))
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(root)],
            env=dict(os.environ, PATH=str(root), PYTHONPATH=str(LIB)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        try:
            if journal_exits:
                _, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 1, err)
                self.assertFalse((root / ".sampler-ready").exists())
            else:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (root / ".sampler-ready").exists() and (
                        root / "gpu.csv"
                    ).read_text().count("2026/") >= 2:
                        break
                    self.assertIsNone(proc.poll(), "sampler exited early")
                    time.sleep(0.05)
                else:
                    self.fail("sampler did not become ready and restart GPU logging")
                proc.terminate()
                _, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, err)
                gpu = (root / "gpu.csv").read_text()
                self.assertEqual(gpu.count("host_epoch"), 1)
                self.assertIn(
                    "nvidia-smi restarts=1", (root / "events.log").read_text()
                )
                rows = list(
                    csv.DictReader((root / "platform.csv").read_text().splitlines())
                )
                self.assertGreater(len(rows), 1)
                self.assertTrue(
                    all(r["ac_online"] == r["broker_turbo"] == "1" for r in rows)
                )
            self.assertIn("NVRM: Xid 79 fixture", (root / "journal.log").read_text())
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=10)

    def test_restart_readiness_and_guard_columns(self):
        self.run_fixture()

    def test_journal_exit_fails_collection(self):
        self.run_fixture(journal_exits=True)


class CollectorStatus(unittest.TestCase):
    def test_child_status_written_before_cleanup(self):
        src = (LIB.parent / "collect.sh").read_text()
        start = src.index("\nfinish() {")
        end = src.index("\n}\n", start) + 3
        with tempfile.TemporaryDirectory() as td:
            for status in ("", "0", "7"):
                script = (
                    src[start:end]
                    + """
RUN_DIR=$1 RUN_ID=fixture START_EPOCH=1 CHILD_RC=$2 FINISHED=0 STARTED=1
TARGET_UID=0 TARGET_GID=0 STOP_REASON=
ev() { :; }
chown() { :; }
stop_sampler() { test -f "$RUN_DIR/metadata.json" || exit 9; }
snapshot() { test -f "$RUN_DIR/metadata.json" || exit 9; }
finish
"""
                )
                subprocess.run(
                    ["bash", "-c", script, "_", td, status],
                    check=True,
                    capture_output=True,
                )
                data = json.loads((Path(td) / "metadata.json").read_text())
                self.assertEqual(
                    data["child"]["exit_status"], int(status) if status else None
                )

    def test_unready_sampler_does_not_launch_workload(self):
        src = (LIB.parent / "collect.sh").read_text()
        guard = next(
            line
            for line in src.splitlines()
            if line.startswith("[[ -e $RUN_DIR/.sampler-ready ]] ||")
        )
        with tempfile.TemporaryDirectory() as td:
            script = (
                "RUN_DIR=$1\ndie() { exit 2; }\n"
                + guard
                + '\ntouch "$RUN_DIR/workload"\n'
            )
            result = subprocess.run(["bash", "-c", script, "_", td])
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(td) / "workload").exists())
