# gpu-power-policy

**Live control of a Linux laptop's NVIDIA GPU power and current limits.** Raise the ceilings
once, then change the active limits in seconds, with the firmware confirming each current
change. Nothing is flashed, runtime overrides release on request, and it is one NixOS module.
On the tested laptop that lifted delivered power 5.8 %, from 117.7 to 124.6 W. Built on
Panchovix's VBIOS editor and loader patch; this repo is the runtime layer.

- Edits four power-policy fields in an in-memory copy of your VBIOS at boot: board-power
  ceiling and default, current ceiling and rated value. The flash chip is never written.
- `gpu-power target 125` and `gpu-power current 107000` take effect in seconds, no reboot
  (`target` restarts `nvidia-powerd`).
- On Dynamic Boost laptops a raised ceiling only takes effect if the target `nvidia-powerd`
  requests moves too. An edited VBIOS alone raises the ceilings; this overrides that target.
- Every current write is confirmed by the firmware's own readback, the CLI refuses to raise
  anything unless the edited image was staged, and `gpu-power rom` exports your own ROM so no
  separate dump tool is needed.

For you if: an NVIDIA laptop GPU with Dynamic Boost (`nvidia-powerd`), NixOS, driver exactly
615.71.09, and load power stuck below the laptop's rated maximum. Experimental; read the
[support boundary](#support-boundary) first.

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
many more GPUs. This repo adds the runtime layer on Linux: a small `nvidia-powerd` preload for
the target and a direct resource-manager client for the current limit, both bound to one GPU
by PCI address.

| | Editor + loader | Editor + loader + this repo |
|---|---|---|
| Raise board-power and current ceilings | yes | yes, same editor |
| Raised ceiling takes effect on a Dynamic Boost laptop | not by itself: `nvidia-powerd` keeps requesting the stock target | yes, target override |
| Change the current limit | edit the image, reboot | in seconds, firmware-verified |
| Change the power target | no | in seconds |
| Release runtime overrides | n/a | `target off`, `current off`, `off` |
| Refuses to raise limits if the image was not staged | n/a | yes |
| Obtain your ROM | separate step (sysfs, nvflash, GPU-Z) | `gpu-power rom` |
| Declarative install | out of scope | NixOS module |
| Windows, GUI, many tested GPUs | yes | no |

## The CLI

All commands run as root and print JSON.

```
gpu-power rom gpu.rom       # export the retained boot ROM to a new private file
gpu-power status            # version, image staging, target state, power readback, policy tables
gpu-power target 125        # set the powerd Dynamic Boost target to 125 W (restarts nvidia-powerd)
gpu-power target off        # remove the override; stock targets resume
gpu-power current 107000    # set the NVVDD current limit to 107 A, verify by readback
gpu-power current off       # restore the configured rated current
gpu-power off               # both of the above: release every runtime override
gpu-power dump DIRECTORY    # save raw policy replies for diagnosis or a bug report
```

A target change lasts until `target off` or reboot; a current change until `current off`,
GPU reinitialisation or reboot. A target must lie between 1 W and the image's power ceiling.
A current must lie between the rated value and the image's current ceiling, narrowed by the
firmware's reported bounds; you cannot go below rated.

## Install on NixOS

Needs `hardware.nvidia.open`, `hardware.nvidia.dynamicBoost.enable`, the `nvidia` video
driver, `allowUnfree`, and a `boot.kernelPackages` whose `nvidiaPackages.latest` is exactly
615.71.09 (nixpkgs `58a20b0a` has it with Linux 7.2.7). Full walkthrough:
[docs/install.md](docs/install.md).

1. **Bootstrap.** Import `gpu-power-policy.nixosModules.default`, enable it with only
   `device = "0000:01:00.0"` (your GPU) and `vbios = null`, rebuild, reboot, then
   `sudo gpu-power rom /var/lib/gpu.rom`.
2. **Configure**, rebuild, reboot:

```nix
hardware.gpuPowerPolicy = {
  enable = true;
  device = "0000:01:00.0";
  vbios = builtins.fetchurl { url = "file:///var/lib/gpu.rom"; sha256 = "<from step 1>"; };
  powerCeilingWatts = 140;            # image ceiling, not an active target
  powerDefaultWatts = 80;             # your ROM's stock boot value
  currentCeilingMilliamps = 120000;   # image ceiling for runtime changes
  currentRatedMilliamps = 99000;      # your ROM's stock value; `current off` restores it
  powerdTargetWatts = 125;            # the one active change at boot; null keeps stock targets
};
```

3. **Verify.** `sudo gpu-power status` must report the image as `selected` and
   `nvidia-smi -q -d POWER` your ceiling as the maximum. On rejection the driver boots the
   stock VBIOS.

The values above are the PT516-52s's; read yours from `nvbios inspect` and keep ceilings
close to stock. Rollback is the previous generation, or `enable = false` and a reboot.

## Other Linux distributions

Coming soon, Fedora first (where the upstream loader is tested). The runtime is plain Python
and C; missing is the install path for the patched module, firmware file and powerd drop-in.

## Support boundary

Known gaps and their intended fixes: [docs/known-gaps.md](docs/known-gaps.md).

- Use on AC, under supervision, with a thermal eye on the first runs. The boot target
  applies on battery too; there is no battery handling yet.
- Hardware-validated: RTX 3080 Ti Laptop GPU (GA103) in the Acer PT516-52s, driver
  615.71.09, Linux 7.2.7. Other Ampere, Ada and Turing laptops have an image path but no
  end-to-end result from this project; Blackwell images are refused.
- The driver version is an exact match, not a series. Policy packet layouts were verified
  against live captures of that build and nothing else.
- The target override acts only through `nvidia-powerd`'s Dynamic Boost request; GPUs
  without it, typically desktop cards, are untested and get no target change.
- Not measured: sustained operation at the image ceilings, games, battery transitions,
  suspend and resume, multi-GPU systems. The limits you set are your responsibility; the
  tested 107 A is a measurement on one board, not a recommendation.

## Checks and bug reports

`nix flake check` runs the CPU tests; `nix build .#kernel-module` builds the patched driver.
For a report, attach `sudo gpu-power dump DIR` (raw policy replies, no personal data),
`nvidia-smi -q -d POWER`, the `vbios_override` kernel log line, and
`nvbios inspect` of your ROM. Earlier work in this space is mapped in the research branch's
`docs/prior-art.md`.

## Credits and licence

MIT. Uses Panchovix's nvidia-vbios-vix-editor (GPL-3.0-or-later, external tool) and the MIT
loader patch from nvidia-linux-vbios-vix-loader; notices and prior art in `NOTICE`.
