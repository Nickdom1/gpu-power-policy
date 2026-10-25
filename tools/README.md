# Checks and experiments

Use these tools from a desktop terminal on AC, with the GPU otherwise idle.
The patched driver and `nvidia-powerd` must already be active; follow the
[installation guide](../docs/install-nixos.md) first. If `turbo-broker` is
installed, enable Turbo. Use a single-user machine, under supervision, with the
laptop on a cooler. Read the [operating limits](../SAFETY.md) before any write.

All commands below run from the repository root.

## Prepare

Install Python 3, GDB, `nvidia-smi`, `journalctl` and `sudo`. Loaded experiments
also require KDE Plasma/Xwayland, `kscreen-doctor`, `xwininfo`, `xprop` and
`libXRes`. They check fullscreen placement on the laptop panel.

Before a loaded experiment, prepare **unfree FurMark 2.10.2** through the pinned
nixpkgs revision used by [shell.nix](../shell.nix):

```sh
./tools/prepare-tools.sh
```

Preparation succeeds when `tools/.tools/furmark/bin/furmark` is available.
Idle and smoke checks do not need FurMark. The root runner reconstructs the
invoking user's desktop environment and launches the workload and display queries
as that user.

## Read the idle state

```sh
sudo bash tools/policy-status/run.sh --idle-only
```

This reads inventory and status without a workload or a write. Inspect the
printed `SEED CHECK` and saved `seed-check.json`: mode 4 must report `PASS`, and
mode 3 must report `stock seeds confirmed`. A completed capture or exit status 0
alone does not mean the seed verdict passed. On refusal or an unexpected state,
stop before writing, retain the printed artifact directory and investigate the
reason using the [diagnostic reference](policy-status/README.md).

## Test one identity write

After the idle verdict passes:

```sh
sudo bash tools/current-limit/run.sh --smoke
```

This writes 99 A back to 99 A to test the transport without changing the intended
setting. Expect `P13 LADDER: COMPLETE` and `identity E61B SET rm_status=0x0`
in the summary. In the saved `result.json`, require `"complete": true`, an empty
`errors` list and `"restore_check": "a61a+a619"`. The smoke summary does not print
a separate restore line. Any failure means stop before loaded experiments and
inspect `summary.txt` and `result.json` in the printed directory.

## Run optional experiments

For the score comparison, boot **mode 4** before running the 107 A commands.
Run four benchmarks at **99 A, 107 A, 107 A and 99 A**, in that order (ABBA).
Wait **at least 90 seconds** between runs. Holds require at least 120 seconds
so both placement checks can complete.

```sh
sudo bash tools/current-limit/run.sh --hold 99000 --seconds 120
# Wait at least 90 seconds after each run.
sudo bash tools/current-limit/run.sh --hold 107000 --seconds 120  # mode 4
# Wait at least 90 seconds.
sudo bash tools/current-limit/run.sh --hold 107000 --seconds 120  # mode 4
# Wait at least 90 seconds.
sudo bash tools/current-limit/run.sh --hold 99000 --seconds 120
```

Compare the four whole-run FurMark scores. A completed loaded run requires the
RTX 3080 Ti renderer; a scored hold also requires a positive integer score and
millisecond duration within 1 second of the requested run.

The causal ladder tests downward pulses, a null write and upward steps. The
following command also requires **mode 4** because it exceeds 99 A:

```sh
sudo bash tools/current-limit/run.sh --cap-ma 114000
```

Mode 3 defaults to a 99 000 mA cap and permits only downward changes and the
99 A reference; larger explicit caps are refused. Preview a schedule without
hardware access by adding `--plan`, for example:

```sh
bash tools/current-limit/run.sh --plan --cap-ma 99000
```

On unsupported desktop placement, the ladder stops before its first write.
A hold writes at +5 seconds, before placement is checked, and attempts restoration
if that check fails. Stop on visual artifacts. `Ctrl-C` stops the workload and
attempts restoration.

## Inspect outputs and recover

Each runner prints `Saved: /var/tmp/...`. Start with `summary.txt` and
`result.json`; loaded experiments also retain telemetry, raw policy snapshots
and control operations. The artifact files are handed back to the invoking user.
See the [current-limit reference](current-limit/README.md) for their layout.

Normal restoration checks both the control block and firmware status
(`a61a+a619`). If it throws an exception, the fallback attempts the setup-time
block and checks only control readback (`a61a-only`). Inspect the errors as well
as the restore field; fallback readback is not full restoration verification.
If restoration fails or remains uncertain, reboot before further experiments.
Rebooting resets runtime current, but leaves the configured boot mode and
installed powerd filter in place. See [levels of undo](../SAFETY.md#levels-of-undo).

For optional firmware investigation, the [boot-VBIOS tools](boot-vbios/README.md)
copy and decode the driver's retained image. They are independent of these
experiments and disabled by default at boot.
