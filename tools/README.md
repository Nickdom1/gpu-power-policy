# Experiment tools

Run from your desktop terminal with `sudo bash …`; artifacts go under `/var/tmp/`.
The root runner reconstructs the invoking user's desktop environment from the
sudo process and user manager, launches FurMark and display queries as that user,
and returns artifact ownership to them. Runtime tools attempt restoration and
report the readback performed; they cannot guarantee recovery.

| Path | Role |
|---|---|
| [current-limit/](current-limit/) | Bounded P13 client, smoke, causal ladder and ABBA hold |
| [policy-status/](policy-status/) | Idle policy/seed check, decoder and display witness |
| [boot-vbios/](boot-vbios/) | Optional boot-image capture and offline decoder |
| `policy-125/guard-boot.sh` | Kernel, driver, PCI, mode and module identity checks |
| `collect.sh`, `bench.sh`, `lib/` | GPU/AC/Turbo telemetry, kernel journal and FurMark launch |

## Requirements and order

The patched module and `nvidia-powerd` must already be active. Install Python 3,
GDB, `nvidia-smi`, `journalctl` and `sudo`. The loaded runs additionally need KDE
Plasma/Xwayland, `kscreen-doctor`, `xwininfo`, `xprop`, `libXRes` and FurMark 2.10.2.
`./prepare-tools.sh` fetches **unfree FurMark** via the pinned nixpkgs revision in
[shell.nix](../shell.nix); prepare it before running experiments. Idle and smoke
checks do not need FurMark. Run on AC; if `turbo-broker` is installed, enable Turbo.

From `tools/`:

1. `sudo bash policy-status/run.sh --idle-only`: read inventory/status and verify the boot seed; no write.
2. `sudo bash current-limit/run.sh --smoke`: one identity write (99 000 → 99 000), exercising the transport.
3. `sudo bash current-limit/run.sh --hold 99000 --seconds 120` and the same at `107000`, in A B B A order, with at least 90 s cooldown: score comparison on mode 4.
4. `sudo bash current-limit/run.sh --cap-ma 114000`: causal ladder on mode 4. Mode 3 defaults to 99 000 (downward/null only); larger explicit caps are refused.

`--plan` prints the schedule without GPU access. The witness checks fullscreen
placement on the laptop panel and the NVIDIA renderer. On an unsupported desktop
it refuses: the ladder stops before its first write; a hold has already written
at +5 s and attempts restoration. `Ctrl-C` also attempts restoration and stops.
