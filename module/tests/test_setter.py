#!/usr/bin/env python3
"""Actual request guard against a synthetic ABI library. Does not load NVML."""

from pathlib import Path
import os
import subprocess
import tempfile
import hashlib
import json
import sys

root = Path(__file__).resolve().parent
lab = root.parent
pin = "5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181"
results = []
with tempfile.TemporaryDirectory(prefix="policy-setter-fixture-") as td:
    t = Path(td)
    so = t / "fixture.so"
    obj = t / "fixture.o"
    subprocess.run(
        ["as", "--64", "-o", str(obj), str(root / "setter-fixture.s")], check=True
    )
    subprocess.run(
        [
            "ld",
            "-m",
            "elf_x86_64",
            "-shared",
            "-Ttext=0x10000",
            "-o",
            str(so),
            str(obj),
        ],
        check=True,
    )
    good = hashlib.sha256(so.read_bytes()).hexdigest()
    script = (lab / "request/request-125.gdb").read_text()
    for case in [
        "positive",
        "rejected",
        "envelope-max",
        "old-max",
        "previous-max",
        "envelope-unexpected",
        "stock-in-envelope",
        "role",
        "cache",
        "type",
        "mask",
        "packet",
        "size",
        "command",
        "value",
        "v2",
        "repeat",
        "wrong-hash",
    ]:
        gdbfile = t / "request.gdb"
        gdbfile.write_text(
            script if case == "wrong-hash" else script.replace(pin, good)
        )
        env = {k: v for k, v in os.environ.items() if k != "PT516_POLICY_MAX_MW"}
        if case in ("envelope-max", "stock-in-envelope"):
            env["PT516_POLICY_MAX_MW"] = "140000"
        fixture_case = "positive" if case == "stock-in-envelope" else case
        run = subprocess.run(
            [
                "gdb",
                "--nx",
                "--batch",
                "-x",
                str(gdbfile),
                "--args",
                sys.executable,
                str(root / "setter-fixture.py"),
                str(so),
                fixture_case,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        log = run.stdout + run.stderr
        assert run.returncode == 0, log
        if case in ("positive", "rejected", "envelope-max"):
            assert (
                "ROLE8 STOP:" not in log
                and "FIXTURE verified exactly one altered-role packet" in log
            ), log
            assert ("status 0x1f" if case == "rejected" else "status 0x0") in log, log
        else:
            assert "ROLE8 STOP:" in log, log
            assert (
                ("ROLE8 request " not in log)
                if case != "repeat"
                else (log.count("ROLE8 request ") == 1)
            ), log
        results.append({"case": case, "passed": True})
        print(case, "PASS")
print(json.dumps(results))
print("%d CPU fixture cases passed; no NVML loaded." % len(results))
