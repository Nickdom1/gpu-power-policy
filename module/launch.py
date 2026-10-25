#!/usr/bin/env python3
"""Initialize the pinned PT516 policy, then exec powerd with its target filter."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / "provenance.json").read_text())
SPEC = importlib.util.spec_from_file_location("boot_log", HERE / "inspect-boot-log.py")
BOOT_LOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOT_LOG)


class Refused(RuntimeError):
    pass


def clean_environment(environment):
    return {
        k: v
        for k, v in environment.items()
        if not k.startswith("NVPCF_SHIM_")
        and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
    }


MAXIMA = {
    3: (125000, 99000),
    4: (140000, 120000),
}  # policy 2 mW, policy 13 mA per image mode


def identity(root=Path("/"), expected_mode=3):
    def read(path):
        return (root / path).read_text().strip()

    pci = root / "sys/bus/pci/devices/0000:01:00.0"
    try:
        owner = (pci / "driver").readlink().name
    except FileNotFoundError:
        owner = "unbound or absent"
    if owner != "nvidia":
        raise Refused(
            "GPU is " + owner + "; NVIDIA ownership is required before initialization"
        )
    module = root / "sys/module/nvidia"
    if not (module / "version").exists():
        raise Refused("NVIDIA module was not loaded")
    mode = module / "parameters/NVreg_Pt516BootPolicy"
    # A normal `switch` may still have the preceding generation's kernel/module.
    # Let stock powerd run in that case; the next ordinary reboot applies this configuration.
    value = mode.read_text().strip() if mode.exists() else None
    if value not in (None, "0", "1", "2", "3", "4"):
        raise Refused("unknown boot policy mode")
    if value != str(expected_mode):
        return {
            "mode": "previous-boot",
            "driver": (module / "version").read_text().strip(),
        }
    values = dict(
        kernel=read("proc/sys/kernel/osrelease"),
        driver=read("sys/module/nvidia/version"),
        module_srcversion=read("sys/module/nvidia/srcversion"),
        vendor=read("sys/bus/pci/devices/0000:01:00.0/vendor"),
        device=read("sys/bus/pci/devices/0000:01:00.0/device"),
        boot_id=read("proc/sys/kernel/random/boot_id"),
        mode=expected_mode,
    )
    srcversion = (
        PIN["module_srcversion"]
        if expected_mode == 3
        else PIN.get("envelope_module_srcversion")
    )
    if not srcversion:
        raise Refused(
            "mode %d module srcversion is not pinned in provenance.json" % expected_mode
        )
    if (
        values["kernel"] != PIN["kernel"]
        or values["driver"] != PIN["driver"]
        or values["module_srcversion"] != srcversion
    ):
        raise Refused(
            "mode %d module/kernel identity differs from the packaged source"
            % expected_mode
        )
    if (values["vendor"], values["device"]) != ("0x10de", "0x2460"):
        raise Refused("PCI target differs")
    return values


def run_command(argv, timeout, environment):
    """Bound the launched process group, including a GDB inferior on timeout."""
    proc = subprocess.Popen(
        [str(a) for a in argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        if proc.returncode:
            raise Refused(
                "command exited %d: %s\n%s" % (proc.returncode, argv[0], output)
            )
        return output
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def setter_succeeded(text):
    required = [
        "ROLE8 trace complete; one 125000 mW role-8 request",
        "ROLE8 RM wrapper returned status 0x0\n",
        "ROLE8 public setter returned NVML status 0\n",
        "set 125.0 W: SUCCESS\n",
        "after: ",
    ]
    return (
        "ROLE8 STOP:" not in text
        and all(s in text for s in required)
        and text.count("ROLE8 request ") == 1
    )


def prepare(args, environment, out):
    def recorded(name, argv, timeout):
        try:
            text = run_command(argv, timeout, environment)
        except Exception as exc:
            text = getattr(exc, "output", None) or str(exc)
            if isinstance(text, bytes):
                text = text.decode(errors="replace")
            (out / name).write_text(text)
            raise
        (out / name).write_text(text)
        return text

    maximum, _ = MAXIMA[args.mode]
    ident = identity(expected_mode=args.mode)
    (out / "identity.json").write_text(json.dumps(ident, indent=2) + "\n")
    if ident["mode"] == "previous-boot":
        print(
            "PT516_GPU_POWER: previous boot module; using stock powerd until the next reboot.",
            flush=True,
        )
        return environment
    if hashlib.sha256(Path(args.nvml).read_bytes()).hexdigest() != PIN["nvml_sha256"]:
        raise Refused("NVML library hash differs")
    # This GET also initializes the GPU if powerd starts before the desktop.
    output = recorded(
        "constraints.log",
        [args.python, HERE / "check-maximum.py", "--max-mw", str(maximum)],
        20,
    )
    print(output, end="", flush=True)
    kernel = recorded("kernel.log", [args.journalctl, "-b", "-k", "--no-pager"], 10)
    if BOOT_LOG.check(kernel, args.mode):
        raise Refused("boot policy preparation/log check failed")
    environment["PT516_POLICY_MAX_MW"] = str(maximum)
    output = recorded(
        "request.log",
        [
            args.gdb,
            "--nx",
            "--batch",
            "-x",
            HERE / "request/request-125.gdb",
            "--args",
            args.python,
            HERE / "request/request-125.py",
        ],
        35,
    )
    del environment["PT516_POLICY_MAX_MW"]
    print(output, end="", flush=True)
    if not setter_succeeded(output):
        raise Refused("role-8 request incomplete or rejected; no retry")
    environment.update(
        LD_PRELOAD=args.shim,
        NVPCF_SHIM_TRACE_PMGR="1",
        NVPCF_SHIM_KERNEL_LIMIT_MW=str(125000),
        NVPCF_SHIM_KERNEL_LIMIT_CAP_MW=str(maximum),
    )
    print(
        "PT516_GPU_POWER: role-8 request returned RM 0x0 / NVML 0; starting powerd with the %d mW target rewrite and %d mW shim cap (mode %d)."
        % (125000, maximum, args.mode),
        flush=True,
    )
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("powerd", "shim", "nvml", "python", "gdb", "journalctl"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", type=int, choices=(3, 4), required=True)
    args = parser.parse_args()
    os.umask(0o077)
    out = Path(tempfile.mkdtemp(prefix="start-", dir="/run/pt516-gpu-power"))

    def stopped(signum, frame):
        raise Refused("interrupted by signal %d" % signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stopped)
    try:
        environment = prepare(args, clean_environment(os.environ), out)
        (out / "result.json").write_text(
            json.dumps(
                dict(
                    prepared=True,
                    epoch=time.time(),
                    target_mw=environment.get("NVPCF_SHIM_KERNEL_LIMIT_MW"),
                    cap_mw=environment.get("NVPCF_SHIM_KERNEL_LIMIT_CAP_MW"),
                )
            )
            + "\n"
        )
        sys.stdout.flush()
        os.execve(args.powerd, [args.powerd], environment)
    except Exception as exc:
        (out / "result.json").write_text(
            json.dumps(dict(prepared=False, error=str(exc), epoch=time.time())) + "\n"
        )
        print(
            "PT516_GPU_POWER: " + str(exc) + "; artifacts: " + str(out),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
