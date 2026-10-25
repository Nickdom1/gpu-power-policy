# Install on NixOS

`module/` is a NixOS module. When enabled it replaces `hardware.nvidia.package` with a patched build of `config.boot.kernelPackages.nvidiaPackages.stable` (the open module), persists `NVreg_Pt516BootPolicy=3` (4 in the envelope) and `NVreg_CaptureBootVbios=0`, and replaces `nvidia-powerd`'s `ExecStart` with the launcher. Use it on AC power only: the installed filter does not preserve the daemon's battery behaviour.

## Requirements

- `hardware.nvidia.open = true`, `hardware.nvidia.dynamicBoost.enable = true`.
- NVIDIA 595.99.02 on Linux 7.2.7 (the module asserts both). The reference system uses `linuxPackages_latest` from a separate nixpkgs pin, `58a20b0a882fffd508ca4dad73836d76632792a3`; the base system/tools pin `ecc58f32…` alone does not supply these versions.
- The reference board (see the README's scope section). On any other image the driver refuses the policy image and the GPU will not initialise while the mode is set.

## Configuration

The repository is a flake whose only output is the module, so it needs no inputs
of its own: it builds against your `boot.kernelPackages` and `pkgs`.

```nix
# flake.nix
{
  inputs.gpu-power-policy.url = "github:<owner>/gpu-power-policy";
  # or a local clone: "git+file:///path/to/gpu-power-policy?ref=release/v0.1"
}
```

```nix
# the host configuration
{ inputs, config, pkgs, ... }:
let
  # The kernel package set that supplies 7.2.7 and NVIDIA 595.99.02.
  kernelPkgs = import inputs.nixpkgs-kernel {
    inherit (pkgs.stdenv.hostPlatform) system;
    config = { inherit (config.nixpkgs.config) allowUnfree; };
  };
in
{
  imports = [ inputs.gpu-power-policy.nixosModules.default ];

  boot.kernelPackages = kernelPkgs.linuxPackages_latest;
  hardware.nvidia.open = true;
  hardware.nvidia.dynamicBoost.enable = true;

  hardware.pt516GpuPower.enable = true;   # off until set; importing alone changes nothing
  hardware.pt516GpuPower.mode = 3;        # 3 = 125 W. 4 also allows the runtime current headroom
  # hardware.pt516GpuPower.captureBootVbios = true;   # optional developer capture, off by default
}
```

Here `inputs.nixpkgs-kernel` is your own second nixpkgs input pinned to
`58a20b0a882fffd508ca4dad73836d76632792a3`; name it whatever you like. Without
flakes, import the path instead (`imports = [ /path/to/gpu-power-policy/module ]`)
and select the same kernel package set by whatever means your configuration uses.

Rebuild, then reboot: the patched module is loaded at boot. If you only `switch`,
the launcher detects the previous boot's module and runs stock `nvidia-powerd`
until the next reboot.

## Choosing the mode

| `mode` | Image | Use |
|---|---|---|
| `3` (default) | Policy 2 maximum 125 W | The 125 W result. Policy 13 stays at its stock 99 A maximum, so `tools/current-limit/` can only hold or lower it |
| `4` | Also policy 13 maximum 120 A and policy 2 maximum 140 W | Needed for a runtime current limit above 99 A. Both rated values are untouched, so every boot still starts at 99 A and a 125 W target; only the runtime tool uses the headroom |

Mode 4 in the ordinary entry is the simplest arrangement: rebuild, reboot, run
the tools. If you would rather keep the default entry's driver byte-identical,
set `envelopeSpecialisation = true` instead and enter the extra `gpu-envelope`
boot entry for one boot with
`sudo bootctl set-oneshot nixos-generation-<N>-specialisation-gpu-envelope.conf`
(do not `nixos-rebuild switch` while booted into it).

## Verify

```sh
journalctl -b -u nvidia-powerd | grep PT516_GPU_POWER
nvidia-smi -q -d POWER | grep -E 'Max Power Limit|Current Power Limit'
sudo cat /run/pt516-gpu-power/start-*/result.json
```

Expected: `role-8 request returned RM 0x0 / NVML 0; starting powerd with the 125000 mW target rewrite`, a maximum of 125 W, and `"prepared": true`.

## Experiment entry points

All from a terminal in the desktop session, on AC, with the GPU otherwise idle (see [tools/README.md](../tools/README.md) for requirements and order):

```sh
sudo bash tools/policy-status/run.sh --idle-only     # one idle capture; no workload, no write
sudo bash tools/current-limit/run.sh --smoke         # identity write; exercises the transport
sudo bash tools/current-limit/run.sh --hold 99000 --seconds 120   # one ABBA arm by FurMark score
```

Each prints a summary and `Saved: /var/tmp/...`. `Ctrl-C` attempts the restore and stops.

## Recovery

| Symptom | Cause | Do |
|---|---|---|
| The GPU does not initialise after enabling (no `nvidia-smi`, journal shows `PT516 boot policy preparation refused`) | The image, PCI identity or checksum did not match | Boot an earlier generation, or add `nvidia.NVreg_Pt516BootPolicy=0` to the kernel command line at the boot menu |
| `nvidia-powerd` failed; `nvidia-smi` shows 80 W | The launcher refused (identity, NVML hash, kernel log or request) | `journalctl -b -u nvidia-powerd` and `sudo cat /run/pt516-gpu-power/start-*/result.json` say why; no target filter is started |
| A tool stopped with `RESTORATION` or `LAST-RESORT` in its summary | The P13 restore could not be verified | Reboot; the boot image seeds 99 A |
| `Device or resource busy` or the GPU vanished mid-run (Xid in the journal) | Fault or reset | Reboot |

## Revert

Set `hardware.pt516GpuPower.enable = false` (or remove the import), rebuild and reboot. Earlier generations are stock.

For reports, use the [issue checklist](../.github/ISSUE_TEMPLATE/problem.md).
