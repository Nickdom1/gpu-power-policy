# Current-limit experiments

The P13 client performs bounded writes with A61A control readback and A619 status
checks. Cleanup attempts to restore 99 A; its summary distinguishes both checks
from A61A-only last-resort readback. See [SAFETY.md](../../SAFETY.md).

| File | Role |
|---|---|
| `p13.py` | 6356-byte control codec: mask `+0x10`, class `+0x96c`, limit `+0x970`; bounds, status checks and schedules |
| `client.py`, `client.gdb` | Pinned NVML transport; refuses unsupported controls, other policies and out-of-window values |
| `ladder.py`, `run.sh` | Preflight, smoke/hold/ladder, telemetry guards, placement, restore attempts and analysis |
| `tests/` | CPU tests against a synthetic NVML-shaped library |

```sh
sudo bash run.sh --smoke
sudo bash run.sh --hold 107000 --seconds 120     # mode 4; one ABBA arm
sudo bash run.sh --cap-ma 114000                 # mode 4; pulses, null, upward rungs
python3 ladder.py --plan --cap-ma 99000          # inspect mode-3 downward/null schedule
```

See the [tool guide](../README.md) for prerequisites and ABBA order. Mode 3 defaults to a
99 000 mA cap (downward/null only); larger explicit caps are refused. Holds require at least
120 s so both placement checks can complete.

Artifacts: `summary.txt`, `result.json`, `identity.json`, `analysis.json` for loaded
runs, `snapshots/`, `rm-ops/` and `MANIFEST.sha256`. Per-window clocks and policy
readings describe the ladder; the whole-run FurMark score is the ABBA outcome.
