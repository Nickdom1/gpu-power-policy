# gpu-power-policy

Experimental Linux tools for NVIDIA laptop GPU power and current limits, without
flashing the VBIOS. The reference system is an Acer Predator Triton 500 SE
(PT516-52s), RTX 3080 Ti Laptop GPU, NVIDIA 595.99.02 with GSP, and Linux 7.2.7.

**Status:** unreleased proof of concept. Hardware results come from one laptop;
the code has changed since and the current tree has CPU validation only.
Use on AC under supervision: the installed powerd filter does not preserve
battery behaviour. Read [SAFETY.md](SAFETY.md) and the
[known issues](CHANGELOG.md#known-issues)
before installation.

## What changed

Raising the board-power target from 120 to 125 W left FurMark at roughly 118 W.
Firmware analysis suggested a separate current constraint. Controlled changes
to that limit tested the explanation and measured its performance effect:

| Experiment | Observation on the reference laptop |
|---|---|
| Boot policy edit and powerd target override | Board-power maximum raised from 115 to 125 W; runtime target set to 125 W |
| Lower the firmware current limit from 99 to 95 A | SM clocks fell 7.4–8.9% in four pulses, then recovered after restoration |
| Raise the current limit to 107 A, keeping the 125 W target | **+6.6% FurMark score** against 99 A: two runs per condition in ABBA order |

The higher current limit shifted the observed constraint toward board power.
These are FurMark results, not measured gaming gains or evidence of a safe
sustained regulator limit. Conditions, individual runs and the decision rule are
in [results](docs/results.md); the [research method and history](docs/history.md)
include failed steps and the Claude-assisted investigation.

## How it works

- **At boot:** a patch to NVIDIA's open kernel module edits an in-RAM copy of the
  card's VBIOS power-policy table before handing it to GSP. A guarded launcher
  requests the higher limit and starts `nvidia-powerd` with a Dynamic Boost
  target filter.
- **At runtime:** an envelope mode also widens policy 13's current maximum while
  leaving its 99 A boot setting unchanged. The experiment client changes that
  limit, verifies readback, and attempts restoration on exit, reporting failures.

Implementation: [module](module/README.md). Experiments and optional inspection: [tools](tools/README.md).
Related projects previously published in-memory VBIOS overrides and powerd
filters; [prior art](docs/prior-art.md) records credits and distinctions.

## Scope and recovery

The image edit and runtime hooks are pinned to the reference board, VBIOS and
library build; exact identities are in [PROVENANCE.md](PROVENANCE.md).
The harness uses KDE Plasma with Xwayland to verify workload placement.

A rejected boot image prevents GPU initialization for that boot. Boot with
`nvidia.NVreg_Pt516BootPolicy=0` to disable the edit. Rebooting resets runtime
current changes, but disabling the installed powerd filter is a separate step;
see [levels of undo](SAFETY.md#levels-of-undo).

Other boards are unsupported, and the shipped capture/decoder tools refuse other
identities. To request investigation, open a board report with your laptop model,
GPU, VBIOS, driver, kernel and stock power limits. No patched-driver installation
or firmware upload is needed for that report.

## Install and check

- [NixOS](docs/install-nixos.md): the reference integration. This repository is a flake whose only output is the module (`inputs.gpu-power-policy.nixosModules.default`); it declares no inputs of its own and builds against yours.
- [Other distributions](docs/install-other-distros.md): untested porting notes. The kernel patch, launcher and target filter are not NixOS-specific; the boot image is pinned to one board, and the NVML hooks need re-pinning (one `objdump` check).

Run the CPU tests and lint checks in the pinned environment:

```sh
nix-shell --run './lint.sh && ./test.sh'
```

These checks do not access the GPU. They do not replace hardware validation.

## License

[MIT](LICENSE), with NVIDIA notices and patch/build terms described there.
Research references are credited in [prior art](docs/prior-art.md).
No NVIDIA firmware, VBIOS images or board schematics are distributed here.
