#!/usr/bin/env python3
"""Actual GDB script against a synthetic CPU library; never loads NVIDIA."""

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

here = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(here))
from codec import (
    EXPECTED,
    INFO_SIZE,
    STATUS_SIZE,
    TYPES,
    inventory,
    status,
    request,
)


def info_blob():
    b = bytearray(INFO_SIZE)
    struct.pack_into("<I", b, 4, 0x7F21F)
    spec = {
        0: (16, 11, 0, 1, 5001000, 5001000),
        1: (3, 12, 0, 1, 1001000, 1001000),
        2: (0, 0, 0, 1000, 80000, 125000),
        3: (15, 11, 0, 1, 5001000, 5001000),
        4: (15, 11, 0, 1, 5001000, 5001000),
        9: (4, 5, 1, 1000, 8000, 8000),
        12: (16, 19, 1, 1000, 5001000, 5001000),
        13: (15, 19, 1, 1, 99000, 99000),
        14: (16, 11, 0, 1, 5001000, 5001000),
        15: (16, 11, 0, 1, 5001000, 5001000),
        16: (4, 18, 0, 10000, 37500, 37500),
        17: (4, 18, 0, 1, 250000, 250000),
        18: (15, 19, 1, 1, 5001000, 5001000),
    }
    for i, (typ, ch, unit, low, rated, high) in spec.items():
        off = 0x58 + 0xE4 * i
        b[off + 4 : off + 7] = bytes([typ, ch, unit])
        struct.pack_into("<III", b, off + 8, low, rated, high)
    return bytes(b)


def status_blob():
    b = bytearray(STATUS_SIZE)
    struct.pack_into("<I", b, 4, 0x7F21F)
    for i, t in TYPES.items():
        off = 0x80 + 0x168C * i
        b[off] = t
        struct.pack_into(
            "<II", b, off + 4, 125000 if i == 2 else 99000, 117600 if i == 2 else 98123
        )
        struct.pack_into("<I", b, off + 28, 125000 if i == 2 else 99000)
        struct.pack_into("<I", b, off + 104, 124000 if i == 2 else 99000)
        struct.pack_into("<I", b, off + 180, 120000 if i == 2 else 99000)
    return bytes(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=False)
    results = []
    info, stat = info_blob(), status_blob()
    decoded = status(stat, info)
    assert len(decoded["policies"]) == 13
    p13 = next(p for p in decoded["policies"] if p["index"] == 13)
    assert (
        p13["record_offset"],
        p13["unit"],
        p13["limit_hint_raw"],
        p13["reading_hint_raw"],
    ) == (0x1259C, "mA", 99000, 98123)
    p2 = next(p for p in decoded["policies"] if p["index"] == 2)
    assert (p2["gsp_type3_output_raw"], p2["pmu_arbiter_output_raw"]) == (
        125000,
        120000,
    )
    assert {p["index"] for p in decoded["policies"]} >= {0, 2, 12, 13, 14, 15, 16, 18}
    assert (
        decoded["image_mode"] == 3
        if "image_mode" in decoded
        else inventory(info)["image_mode"] == 3
    )
    # Envelope inventory (mode 4): both maxima widened, rated unchanged; anything else is refused.
    env = bytearray(info)
    struct.pack_into("<I", env, 0x58 + 2 * 0xE4 + 16, 140000)
    struct.pack_into("<I", env, 0x58 + 13 * 0xE4 + 16, 120000)
    assert inventory(bytes(env))["image_mode"] == 4
    for bad in [(2, 16, 150000), (13, 16, 125000), (13, 12, 120000), (2, 12, 90000)]:
        i, field, value = bad
        b = bytearray(env)
        struct.pack_into("<I", b, 0x58 + i * 0xE4 + field, value)
        try:
            inventory(bytes(b))
        except ValueError:
            pass
        else:
            raise AssertionError("unexpected inventory accepted: %r" % (bad,))
    cmd, payload = request("status", info)
    assert cmd == 0x2080A619 and payload[4:8] == bytes.fromhex("1ff20700")
    assert not any(payload[:4] + payload[8:])
    for data, which in [(info[:-1], "info"), (stat[:-1], "status")]:
        try:
            inventory(data) if which == "info" else status(data, info)
        except ValueError:
            pass
        else:
            raise AssertionError("short packet accepted")
    results.append(
        {
            "case": "offline types, units, full mask, bounds and conflicting raw outputs",
            "passed": True,
        }
    )
    assembler = shutil.which("as") or "as"
    linker = shutil.which("ld") or "ld"
    gdb = shutil.which("gdb") or "gdb"
    python = sys.executable
    with tempfile.TemporaryDirectory(prefix="policy-status-fixture-") as tmp:
        tmp = Path(tmp)
        obj = tmp / "fixture.o"
        so = tmp / "fixture.so"
        subprocess.run(
            [assembler, "--64", "-o", str(obj), str(here / "tests/fixture.s")],
            check=True,
        )
        subprocess.run(
            [
                linker,
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
        (tmp / "codec.py").write_text(
            (here / "codec.py")
            .read_text()
            .replace(EXPECTED, hashlib.sha256(so.read_bytes()).hexdigest())
        )
        stops = {
            "wrong-hash": "loaded NVML hash differs",
            "setter": "public setter reached",
            "v2": "public setter reached",
            "size": "unexpected A618 caller",
            "caller": "unexpected A618 caller",
            "callback": "RM callback command",
            "privileged": "privileged legacy control",
            "missing": "no complete result",
            "pid": "buffer descriptor",
            "buffer-size": "buffer descriptor",
            "pointer": "invalid buffer pointer",
            "overlap": "overlaps",
            "pristine": "not pristine",
            "pre-guard": "not pristine",
            "post-guard": "buffer guard",
            "source-mutated": "buffer guard",
            "unsupported": "nonzero RM status",
            "wrong-mask": "policy mask differs",
            "wrong-class": "class differs",
            "wrong-max": "match neither the 125 W image nor the envelope",
            "wrong-unit": "type/unit differs",
        }
        cases = [
            ("info", "positive"),
            ("info", "envelope"),
            ("info", "wrong-max"),
            ("info", "wrong-unit"),
            ("status", "positive"),
        ]
        cases += [("status", m) for m in stops if m not in ("wrong-max", "wrong-unit")]
        for query, mode in cases:
            sample = root / (query + "-" + mode)
            dest = sample / query
            dest.mkdir(parents=True)
            if query == "status":
                (sample / "info").mkdir()
                (sample / "info/a618.bin").write_bytes(info)
            blob = bytearray(info if query == "info" else stat)
            if mode == "wrong-max":
                struct.pack_into("<I", blob, 0x230, 115000)
            elif mode == "envelope":
                struct.pack_into("<I", blob, 0x230, 140000)
                struct.pack_into("<I", blob, 0x58 + 13 * 0xE4 + 16, 120000)
            elif mode == "wrong-unit":
                blob[0xBF2] = 0
            elif mode == "wrong-mask":
                struct.pack_into("<I", blob, 4, 0x52004)
            elif mode == "wrong-class":
                blob[0x1259C] = 0
            payload = sample / "payload.bin"
            payload.write_bytes(blob)
            env = dict(
                os.environ,
                PT516_POLICY_DIR=str(dest),
                PT516_POLICY_LAB=str(here if mode == "wrong-hash" else tmp),
                PT516_POLICY_QUERY=query,
                PYTHONDONTWRITEBYTECODE="1",
            )
            r = subprocess.run(
                [
                    gdb,
                    "--nx",
                    "--batch",
                    "-x",
                    str(here / "capture.gdb"),
                    "--args",
                    python,
                    str(here / "tests/fixture.py"),
                    str(so),
                    mode,
                    str(payload),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            text = r.stdout + r.stderr
            (dest / "trace.log").write_text(text)
            assert r.returncode == 0, text
            if mode in ("positive", "envelope"):
                assert (
                    "POLICY STOP:" not in text and "POLICY capture complete;" in text
                ), text
                name = "a618" if query == "info" else "a619"
                rec = json.loads((dest / (name + ".json")).read_text())
                if mode == "envelope":
                    assert (
                        json.loads((dest / "decoded.json").read_text())["image_mode"]
                        == 4
                    )
                assert (
                    rec["callback_count"] == 1
                    and rec["buffer_guards_intact"]
                    and rec["original_a618_buffer_unchanged"]
                )
                assert (dest / (name + ".bin")).read_bytes() == blob
                assert (dest / "decoded.json").exists()
            else:
                assert stops[mode] in text and "POLICY capture complete;" not in text, (
                    text
                )
                assert not (dest / "decoded.json").exists()
                if mode in (
                    "post-guard",
                    "source-mutated",
                    "unsupported",
                    "wrong-mask",
                    "wrong-class",
                    "wrong-max",
                    "wrong-unit",
                ):
                    assert (
                        dest / ("a618.bin" if query == "info" else "a619.bin")
                    ).exists(), text
            results.append({"case": query + "-" + mode, "passed": True})
            print(query, mode, "PASS", flush=True)
    (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(len(results), "CPU capture checks passed; no NVIDIA library loaded")


if __name__ == "__main__":
    main()
