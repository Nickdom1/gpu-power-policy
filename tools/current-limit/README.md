# Current-limit implementation

The client runs bounded experiments on firmware policy 13 (the core-current
policy, or P13). Follow the [tool guide](../README.md) for preparation, idle
inspection, the identity-write smoke test and experiment commands.

## Control and verification

The control block is 6356 bytes: policy mask at `+0x10`, class at `+0x96c` and
current limit at `+0x970`. Only policy 13 may be written. Python and the GDB hook
both enforce a 94 000 mA floor and the run cap, which cannot exceed the booted
image's maximum. Mode 3 permits at most 99 000 mA; mode 4 permits 120 000 mA.
These bounds do not establish a safe sustained current.

Each write starts with a fresh control-block read (A61A), refuses unrelated
changes, sets the limit (E61B), compares control readback and checks firmware
status (A619). The runner holds a single-writer lock. On exit it attempts to
restore 99 A. After a normal-restore exception, the fallback writes the
setup-time block and checks A61A only; it does not check A619.
See [restoration and recovery](../../SAFETY.md#runtime-restoration).

A completed loaded run requires the RTX 3080 Ti renderer. Scored holds also
require positive integer score/duration fields, a duration expressed in `ms`,
and a duration within 1 second of the request. Startup may precede renderer
output; completion may not.

## Source files

| File | Role |
|---|---|
| `p13.py` | Control codec, bounds, status checks and schedules |
| `client.py`, `client.gdb` | Pinned NVML transport and write guards |
| `ladder.py`, `run.sh` | Preflight, smoke/hold/ladder, telemetry guards, placement, restoration and analysis |
| `tests/` | CPU fixtures for transport, control, restoration and benchmark validation |

The runner imports the inventory/status decoder and display helpers from
[policy-status/](../policy-status/README.md). Telemetry and workload launch use
`tools/collect.sh`, `tools/bench.sh` and `tools/lib/`.

## Saved artifacts

`summary.txt` and `result.json` record completion, errors and restoration checks.
`identity.json` records the boot, library identity and schedule. Loaded runs add
`analysis.json`; `snapshots/` and `rm-ops/` retain policy reads and control
operations, and `p13-ladder/` holds workload telemetry.
`tool-hashes.json` identifies the source files; `MANIFEST.sha256` hashes outputs.

Per-window clocks and policy readings describe the ladder. The whole-run
FurMark score is the outcome for each ABBA hold; it is not a score for an
individual ladder window. Historical outputs in `docs/results/` retain their
original format and reporting limitations.
