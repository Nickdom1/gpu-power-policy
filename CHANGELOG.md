# Changelog

## Unreleased (proof of concept, 2026-10-24)

Research snapshot from one PT516-52s with NVIDIA 595.99.02 / Linux 7.2.7. The hardware runs predate the portability edits; those are CPU-tested only.

- Boot-time in-RAM VBIOS policy edit (board-power maximum 115 → 125 W) through the driver's registry `vbios` key; launcher with identity checks, role-8 request and `nvidia-powerd` target filter.
- Envelope image mode 4 (policy 2 maximum 140 W, policy 13 maximum 120 A, rated values unchanged).
- Runtime P13 client with bounded writes, readback verification and restore; ladder and hold modes.
- Policy-status capture and decoder; telemetry harness; offline VBIOS table inspector.
- Measured: P13 is the binding limit at the 125 W plateau; 107 A vs 99 A gives +6.6 % FurMark score.

- Minimal release: removed the old offline analysis, loaded policy-status series,
  broad system telemetry, MangoHud and duplicate metadata/evidence. Retained the
  causal ladder, ABBA holds, placement witness and optional boot-VBIOS tools;
  capture is disabled by default. Fixed the NVML pin bypass, sampler readiness
  gate and ambiguous restore reporting.
- CPU validation only. The reduced sampler/collector and FurMark launcher,
  startup `--mode` interface, shim cleanup, runtime changes and capture default
  have not been retested on the laptop. Kernel patches and transport hooks are unchanged.

### Known issues

- **Adapter re-initialisation.** The kernel patch refuses to register its policy image if the per-GPU registry already holds a `vbios` entry. Driver shutdown without PCI removal keeps that registry (for example the last device close with no persistence daemon and `nvidia-drm modeset=0`, or GPU-reset recovery), so the next initialisation fails until the module is reloaded or the machine rebooted. Not seen on the reference system, where the compositor keeps the device open. Planned fix: treat an identical existing entry as success.
- **Battery.** The powerd filter rewrites the Dynamic Boost target unconditionally; it does not distinguish AC from battery. Use the tools and the 125 W configuration on AC only.
- **Identity pins live in several places** (`module/provenance.json`, read by `module/launch.py`; `tools/policy-125/guard-boot.sh`; the NVML hash also in `module/check-maximum.py`, `module/request/request-125.gdb` and `tools/policy-status/codec.py`). Other builds need offset validation as well as new identity pins; see the porting notes.
- **Harness portability.** The X11 witness looks up `xwininfo`, `xprop` and `libXRes` on `PATH` or by soname; untested outside NixOS and KDE Plasma.
- **Multi-user machines.** Artifact directories under `/var/tmp` are handed to the invoking user while root still writes into them; assume a single-user machine.
- **Driver polish.** Mode 0 is not byte-identical to stock (an assertion became a hard error, an overflow guard was added to the registry RPC in `rpc.c`, the GSP message queue was enlarged, and the capture hook is compiled in); the success log line uses the error level; new files lack copyright lines; the filter has no fallback if `dlsym` fails.
- **Capture tooling is pinned.** `tools/boot-vbios/collect-boot-vbios.py` refuses any GPU other than 10de:2460 at `0000:01:00.0` and any module `srcversion` not listed in `module/provenance.json`; `inspect-saved-vbios.py` refuses a base image with another PCI identity. Planned: an `--allow-other-board` mode that records the identity instead of refusing, so board reports can use the shipped tools.
- **Other distributions.** The install guide is untested; the NVML hash pin and the GDB hook offsets come from the nixpkgs build of the library and require independent validation before re-pinning. The tools read NVML from a NixOS path unless overridden.
