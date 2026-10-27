import json
import pathlib
import re
import sys


def require(ok, message):
    if not ok:
        sys.exit("linux.sh: " + message)


config_path, work = map(pathlib.Path, sys.argv[1:])
c = json.loads(config_path.read_text())
keys = ["powerCeilingWatts", "powerDefaultWatts", "currentCeilingMilliamps", "currentRatedMilliamps"]
require(
    isinstance(c, dict) and not (c.keys() - set(keys + ["device", "vbios", "powerdTargetWatts"])),
    "unknown configuration key",
)
gpus = [
    p.name
    for p in pathlib.Path("/sys/bus/pci/devices").iterdir()
    if (p / "vendor").read_text().strip() == "0x10de" and int((p / "class").read_text(), 16) >> 16 == 3
]
bdf = c.get("device")
if bdf is None:
    require(len(gpus) == 1, "set device explicitly when there is not exactly one NVIDIA GPU")
    bdf = gpus[0]
require(
    isinstance(bdf, str) and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", bdf) and bdf in gpus,
    "invalid NVIDIA GPU device",
)
rom = c.get("vbios")
if rom is not None:
    require(isinstance(rom, str) and rom and "\n" not in rom and "\r" not in rom, "invalid vbios path")
    rom = str((config_path.resolve().parent / rom).resolve(strict=True))
    require("\n" not in rom and "\r" not in rom, "invalid resolved vbios path")
    require(
        all(type(c.get(k)) is int and 1 <= c[k] <= 4294967 for k in keys), "configured mode needs four positive limits"
    )
    require(c[keys[1]] <= c[keys[0]] and c[keys[3]] <= c[keys[2]], "defaults must not exceed ceilings")
t = c.get("powerdTargetWatts")
require(t is None or (rom and type(t) is int and 1 <= t <= c[keys[0]]), "invalid boot powerd target")
runtime = {
    "device": bdf,
    "modinfo": "/usr/sbin/modinfo",
    "systemctl": "/usr/bin/systemctl",
    "journalctl": "/usr/bin/journalctl",
    "dmesg": "/usr/bin/dmesg",
    "nvidiaSmi": "/usr/bin/nvidia-smi",
}
if rom:
    runtime.update({k: c[k] for k in keys})
    runtime["powerdTargetWatts"] = t
    (work / "changes.json").write_text(
        json.dumps(
            dict(
                zip(  # noqa: B905
                    ["power_max_w", "power_default_w", "nvvdd_output_ocp_max_ma", "nvvdd_output_ocp_rated_ma"],
                    [c[k] for k in keys],
                )
            )
        )
    )
(work / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
(work / "settings").write_text(
    "\n".join([bdf, rom or "", str(c[keys[0]] * 1000) if rom else "", str(t * 1000) if t else ""]) + "\n"
)
c.update(device=bdf, vbios=rom)
(work / "install.json").write_text(json.dumps(c, indent=2) + "\n")
