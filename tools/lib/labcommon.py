"""Shared helpers for the PT516 GPU power lab. Python 3 standard library only.

Conventions used by every tool in this directory:
- host_epoch is float seconds since the Unix epoch (time.time()). For
  platform rows it is when the sample was read; for GPU rows it is when the
  nvidia-smi line arrived on the pipe (a few milliseconds after its own
  timestamp column).
- A missing reading is None. None is never treated as zero.
- events.log is tab separated: local ISO time, epoch, source, message.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

HARNESS_VERSION = "gpu-power-policy tools 0.2"

# Strings nvidia-smi and sysfs use for "no reading". Words such as "none"
# (no holders) and "Unknown" (a battery status) are values, not gaps.
NA_STRINGS = {
    "",
    "n/a",
    "na",
    "nan",
    "not supported",
    "[n/a]",
    "[not supported]",
    "[unknown error]",
    "[gpu is lost]",
    "[insufficient permissions]",
    "[requested functionality has been deprecated]",
}

GPU_HEADER_ALIASES = {
    "clocks.current.graphics": "clocks.gr",
    "clocks.current.sm": "clocks.sm",
    "clocks.current.memory": "clocks.mem",
    "clocks.current.video": "clocks.video",
    "gpu.power.draw.average": "power.draw.average",
    "gpu.power.draw.instant": "power.draw.instant",
    "gpu.power.requested_limit": "power.limit",
    "gpu.power.current_limit": "enforced.power.limit",
    "gpu.power.default_limit": "power.default_limit",
    "gpu.power.min_limit": "power.min_limit",
    "gpu.power.max_limit": "power.max_limit",
}


def parse_value(text):
    """Parse one CSV cell into int, float, str or None.

    "Active"/"Not Active" become 1/0 and "0x..." masks become int, so the
    clock event reason columns can be summed directly.
    """
    if text is None:
        return None
    t = str(text).strip()
    low = t.lower()
    if low in NA_STRINGS or (t.startswith("[") and t.endswith("]")):
        return None
    if t == "Active":
        return 1
    if t == "Not Active":
        return 0
    if low.startswith("0x"):
        try:
            return int(t, 16)
        except ValueError:
            return None
    try:
        v = float(t)
    except ValueError:
        return t
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def norm_gpu_header(name):
    """'clocks.current.sm [MHz]' -> 'clocks.sm'; throttle -> event names."""
    h = re.sub(r"\s*\[[^\]]*\]\s*$", "", name.strip())
    if h.startswith("clocks_throttle_reasons."):
        h = "clocks_event_reasons." + h.split(".", 1)[1]
    return GPU_HEADER_ALIASES.get(h, h)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def iso_local(epoch):
    lt = time.localtime(epoch)
    ms = int((epoch - math.floor(epoch)) * 1000)
    off = time.strftime("%z", lt)
    if len(off) == 5:
        off = off[:3] + ":" + off[3:]
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{ms:03d}" + off


def event_line(epoch, source, message):
    msg = " ".join(str(message).split())
    return f"{iso_local(epoch)}\t{epoch:.3f}\t{source}\t{msg}\n"


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v)
