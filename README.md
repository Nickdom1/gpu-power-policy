# gpu-power-policy

**Live control of a Linux laptop's NVIDIA GPU power and current limits.** Raise the ceilings
once, then change the active limits in seconds, with the firmware confirming each current
change. Nothing is flashed, runtime overrides release on request, and it installs as one
NixOS module or one script on Fedora and Ubuntu. On the tested laptop that lifted delivered
power 5.8 %, from 117.7 to 124.6 W. Built on Panchovix's VBIOS editor and loader patch; this
repo is the runtime layer.

- Edits four power-policy fields in an in-memory copy of your VBIOS at boot: board-power
  ceiling and default, current ceiling and rated value. The flash chip is never written.
- On a Dynamic Boost laptop a raised ceiling does nothing by itself, because `nvidia-powerd`
  keeps requesting the stock target. This overrides that target, and the current limit behind
  it, from the command line in seconds.
- Every current write is confirmed by the firmware's own readback, and `gpu-power rom`
  exports your own ROM, so no separate dump tool is needed.

For you if: an NVIDIA laptop GPU (desktop cards untested), driver exactly 615.71.09, and
load power stuck below its rated maximum, on NixOS, Fedora 43 or Ubuntu 24.04. Experimental;
read the [support boundary](#support-boundary) first.

## Results

Same laptop, same driver, FurMark 2.10.2 Vulkan at 2560×1440 fullscreen on the laptop panel,
120 s per run, AC power, external cooler, Turbo profile on, 125 W board-power target
throughout. Only the NVVDD (GPU core rail) current limit changed, in A-B-B-A order so drift
would show. Power is the mean over seconds 20–110; the score covers the whole run.

| Run | Current limit | Mean GPU power | FurMark score |
|---|---:|---:|---:|
| A1 | 99 A (stock) | 117.7 W | 10 703 |
| B1 | 107 A | 124.6 W | 11 352 |
| B2 | 107 A | 124.6 W | 11 352 |
| A2 | 99 A (stock) | 117.8 W | 10 741 |
| Control, Turbo off | 99 A (stock) | 117.8 W | 10 704 |

**+5.8 % delivered power, +5.9 % score**, repeats within 0.4 % (the two 107 A runs scored
identically), peak GPU temperature 58 °C. The control run shows the balanced profile keeps
the 125 W target; 107 A with Turbo off was not tested.

On 595.99.02 the same plateau held (112 W stock against a 115 W maximum, about 118 W at a
125 W target), and a 95 A limit lowered clocks 7–9 %: the current limit is enforced.

Per-run record: [2026-10-25-615-abba.json](docs/evidence/2026-10-25-615-abba.json).

## How it fits with Panchovix's tools

The VBIOS edit uses [nvidia-vbios-vix-editor](https://github.com/Panchovix/nvidia-vbios-vix-editor)
unmodified, and the in-memory load is the MIT kernel patch from
[nvidia-linux-vbios-vix-loader](https://github.com/Panchovix/nvidia-linux-vbios-vix-loader).
Those are the right choice if a fixed edited image is all you need; they cover Windows and
many more GPUs. This repo adds the runtime layer on Linux: an `nvidia-powerd` preload for the
target and a resource-manager client for the current limit, both bound to one GPU by address.

| | Editor + loader | Editor + loader + this repo |
|---|---|---|
| Raise board-power and current ceilings | yes | yes, same editor |
| Raised ceiling takes effect on a Dynamic Boost laptop | not by itself: `nvidia-powerd` keeps requesting the stock target | yes, target override |
| Change the current limit | edit the image, reboot | in seconds, firmware-verified |
| Change the power target | no | in seconds |
| Raise the limit without Dynamic Boost (VM, desktop) | no | `gpu-power limit`, direct firmware request |
| Release runtime overrides | n/a | `target off`, `current off`, `off` |
| Refuses to raise limits if the image was not staged | n/a | yes |
| Obtain your ROM | separate step (sysfs, nvflash, GPU-Z) | `gpu-power rom` |
| Declarative install | out of scope | NixOS module, or one script on Fedora and Ubuntu |
| Windows, GUI, many tested GPUs | yes | no |

## The CLI

All commands run as root and print JSON.

```
gpu-power rom gpu.rom       # export the retained boot ROM to a new private file
gpu-power status            # version, image staging, target state, power readback, policy tables
gpu-power target 125        # set the powerd Dynamic Boost target to 125 W (restarts nvidia-powerd)
gpu-power target off        # remove the override; stock targets resume
gpu-power limit 125         # request 125 W through NVML, falling back to the firmware limit request
gpu-power limit off         # restore the configured stock power limit
gpu-power current 107000    # set the NVVDD current limit to 107 A, verify by readback
gpu-power current off       # restore the configured rated current
gpu-power off               # restore rated current, remove the target and restore the stock limit
gpu-power dump DIRECTORY    # save raw policy replies for diagnosis or a bug report
```

Changes last until released, the GPU reinitialises or you reboot. Values are bounded by the
image's ceilings and the firmware's own reported bounds; you cannot go below rated.

## Install

Two rounds either way: the first boot exports your ROM, the second applies the edited image.
Prerequisites, the full option list, verification and rollback are in
[docs/install.md](docs/install.md).

```nix
hardware.gpuPowerPolicy = {
  enable = true;
  device = "0000:01:00.0";            # your GPU
  vbios = null;                       # first round: export only
  powerCeilingWatts = 140;            # image ceiling, not an active target
  currentCeilingMilliamps = 120000;
  powerdTargetWatts = 125;            # the one active change at boot
};
```

On Fedora and Ubuntu the same fields live in a JSON file, and Debian-family derivatives use
that path unchanged: `sudo bash install/linux.sh gpu-power.json`.

## Support boundary

Validated on one machine: an RTX 3080 Ti Laptop GPU in an Acer PT516-52s, driver exactly
615.71.09. Run it on AC, supervised, and watch temperatures on the first runs. What is
untested, what the limits mean and what the known failure modes are:
[docs/known-gaps.md](docs/known-gaps.md).

## Checks and bug reports

`python3 -m unittest discover -s tests` runs the CPU suites, as CI does on every push;
`nix flake check` adds the Nix evaluations and `nix build .#kernel-module` builds the patched
driver. For a report attach `sudo gpu-power dump DIR`, `nvidia-smi -q -d POWER` and the
`vbios_override` kernel log line. The research this grew out of, with its dated history and a
map of related work, is preserved on the
[research/v0.1 branch](https://github.com/Nickdom1/gpu-power-policy/tree/research/v0.1).

## Credits and licence

MIT. Uses Panchovix's nvidia-vbios-vix-editor (GPL-3.0-or-later, external tool) and the MIT
loader patch from nvidia-linux-vbios-vix-loader; notices and prior art in `NOTICE`.
