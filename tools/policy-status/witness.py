#!/usr/bin/env python3
"""Desktop-user workload wrapper. No NVML/RM imports; only X11 reads and /proc."""

import json
import shutil
import os
from pathlib import Path
import re
import signal
import sys
import time
import subprocess
from run import Stop, command, stop_owned, dump, bench_epoch

XWININFO = Path(shutil.which("xwininfo") or "xwininfo")
XPROP = Path(shutil.which("xprop") or "/run/current-system/sw/bin/xprop")


def geometry(text):
    names = {
        "x": "Absolute upper-left X",
        "y": "Absolute upper-left Y",
        "width": "Width",
        "height": "Height",
    }
    out = {}
    for key, label in names.items():
        m = re.search(r"^\s*" + label + r":\s*(-?\d+)\s*$", text, re.M)
        if not m:
            raise Stop("xwininfo geometry missing " + key)
        out[key] = int(m[1])
    return out


def display_config(d):
    # Ignore ephemeral mode IDs, available-mode list and brightness.
    out = []
    for item in d["outputs"]:
        if not item.get("connected") or not item.get("enabled"):
            continue
        mode = next(m for m in item["modes"] if m["id"] == item["currentModeId"])
        out.append(
            dict(
                name=item["name"],
                pos=item["pos"],
                scale=item["scale"],
                rotation=item["rotation"],
                size=mode["size"],
                refresh=round(mode["refreshRate"], 2),
            )
        )
    return sorted(out, key=lambda x: x["name"])


def capture(dest, owner_pid):
    dest.mkdir()
    deadline = time.monotonic() + 8

    def read(name, args, required=True):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Stop("desktop witness exceeded 8-second capture deadline")
        rc, text = command(args, timeout=min(3, remaining))
        (dest / name).write_text(text)
        if rc:
            if required:
                raise Stop("desktop witness failed: " + name)
            return None
        return text

    tree = read("tree.txt", [XWININFO, "-root", "-tree"])
    # WM_NAME can be absent even for the visible benchmark window. Enumerate
    # XIDs, then prove process ownership; a title is neither required nor trusted.
    candidates = list(dict.fromkeys(re.findall(r"^\s*(0x[0-9a-fA-F]+)\s+", tree, re.M)))
    if len(candidates) > 256:
        raise Stop("desktop witness window census exceeds 256 candidates")
    # Require PID ownership by the wrapper's own workload.
    from run import process_table

    table = process_table()
    owned = {owner_pid}
    while True:
        more = {pid for pid, (parent, _) in table.items() if parent in owned}
        if more <= owned:
            break
        owned |= more
    found = []
    server_owners = None
    for wid in candidates:
        prop = read(
            wid + "-properties.txt",
            [XPROP, "-id", wid, "_NET_WM_PID", "_NET_WM_STATE"],
            required=False,
        )
        # An unrelated window can disappear between the census and property read.
        # Missing properties never establish ownership; the exact-one guard stays.
        if prop is None:
            continue
        pid = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", prop)
        source = "_NET_WM_PID"
        window_pid = int(pid[1]) if pid else None
        if window_pid is None:
            # Some Vulkan clients publish neither legacy title nor PID. Ask the
            # X server for the local connection owner; never infer it from size.
            if server_owners is None:
                census = read(
                    "xres-owners.json",
                    [
                        sys.executable,
                        Path(__file__).with_name("xclient_pid.py"),
                        *candidates,
                    ],
                )
                server_owners = json.loads(census)["owners"]
            window_pid = server_owners.get(wid)
            source = "XRes-local-client"
        if window_pid in owned:
            text = read(wid + "-geometry.txt", [XWININFO, "-id", wid])
            if "IsViewable" in text:
                found.append(
                    dict(
                        window=wid,
                        pid=window_pid,
                        pid_source=source,
                        fullscreen="_NET_WM_STATE_FULLSCREEN" in prop,
                        geometry=geometry(text),
                    )
                )
    if len(found) != 1:
        raise Stop(
            "expected exactly one visible FurMark window owned by this workload; found %d among %d X11 candidates"
            % (len(found), len(candidates))
        )
    config = json.loads(read("kscreen.json", ["kscreen-doctor", "-j"]))
    result = dict(
        epoch=time.time(),
        window=found[0],
        displays=display_config(config),
        interpretation="X11 window geometry only; does not prove Vulkan render/swapchain extent or physical placement.",
    )
    dump(dest / "result.json", result)
    return result


def publish_result(out, result):
    # The supervisor reads this concurrently before interpreting falling load.
    pending = out / "result.pending.json"
    dump(pending, result)
    pending.replace(out / "result.json")


def main():
    out = Path(os.environ["LAB_RUN_DIR"]) / "witness"
    out.mkdir()
    proc = None

    def interrupted(signum, frame):
        raise Stop("desktop witness interrupted")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    # Defaults reproduce the reference 120 s panel runs; the ladder/ABBA runners set these.
    seconds = int(os.environ.get("PT516_WITNESS_SECONDS", "120"))
    try:
        proc = subprocess.Popen(
            [
                "bash",
                str(Path(__file__).resolve().parent.parent / "bench.sh"),
                "furmark",
                "--seconds",
                str(seconds),
                "--api",
                "vk",
                "--res",
                "2560x1440",
            ],
            start_new_session=True,
        )
        start = time.monotonic()
        captured = set()
        begin = None
        while proc.poll() is None:
            now = time.time()
            if begin is None:
                begin = bench_epoch(Path(os.environ["LAB_RUN_DIR"]) / "events.log")
            for delay in (30, 90):
                if begin is not None and now - begin >= delay and delay not in captured:
                    capture(out / ("geometry-%ds" % delay), proc.pid)
                    captured.add(delay)
            if time.monotonic() - start > seconds + 30:
                raise Stop("desktop workload deadline exceeded")
            time.sleep(1)
        if proc.returncode or captured != {30, 90}:
            raise Stop("workload or desktop witness incomplete")
        publish_result(out, dict(complete=True))
        return 0
    except Exception as exc:
        publish_result(out, dict(complete=False, error=str(exc)))
        print("WITNESS STOP: " + str(exc), flush=True)
        return 1
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        stop_owned(proc)


if __name__ == "__main__":
    sys.exit(main())
