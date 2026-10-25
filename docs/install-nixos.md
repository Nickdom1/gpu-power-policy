# Install on NixOS

This module installs the patched NVIDIA open driver and a launcher that starts
`nvidia-powerd` with a 125 W target filter. Importing it alone changes nothing;
`hardware.pt516GpuPower.enable = true` enables the configuration.

Use on AC only: the installed filter does not preserve battery behavior. Keep
a boot generation known to use the stock driver available for recovery. Read
[safety](../SAFETY.md) and the [validation status](../PROVENANCE.md#validation)
before enabling it.

## Requirements

- The [reference board and VBIOS](../PROVENANCE.md#historical-hardware-configuration).
  Another image is refused and the GPU will not initialize while the edit is enabled.
- NVIDIA 595.99.02 and Linux 7.2.7; the module asserts both. The reference system
  uses `linuxPackages_latest` from nixpkgs `58a20b0a882fffd508ca4dad73836d76632792a3`.
  The base/tools pin `ecc58f32…` alone does not supply these versions.
- `hardware.nvidia.open = true` and `hardware.nvidia.dynamicBoost.enable = true`.

## Enable the module

The repository flake exports a NixOS module. It declares no inputs of its own
and builds against your `boot.kernelPackages` and `pkgs`. Add these inputs to
your system flake:

```nix
{
  inputs.gpu-power-policy.url = "github:Nickdom1/gpu-power-policy";
  inputs.nixpkgs-kernel.url = "github:NixOS/nixpkgs/58a20b0a882fffd508ca4dad73836d76632792a3";
  # Or use a local clone: "git+file:///path/to/gpu-power-policy?ref=release/v0.1"
}
```

In the `nixpkgs.lib.nixosSystem` call that defines your host, pass the flake's
`inputs` through `specialArgs = { inherit inputs; };`. This makes `inputs`
available to the host configuration below:

```nix
{ inputs, config, pkgs, ... }:
let
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

  hardware.pt516GpuPower.enable = true;
  hardware.pt516GpuPower.mode = 3;
  # hardware.pt516GpuPower.captureBootVbios = true;
}
```

Without flakes, import `/path/to/gpu-power-policy/module` and select the same
kernel package set. Optional `captureBootVbios` retains the original image for
the [inspection tools](../tools/boot-vbios/README.md); it is off by default.

## Choose the mode

| `mode` | Board-power maximum | Current maximum | Runtime use |
|---|---|---|---|
| `3` (default) | 125 W | 99 A | Hold or lower current from 99 A |
| `4` | 140 W | 120 A | Required for experiments above 99 A |

Both modes preserve the rated values, start current at 99 A and request a
125 W board-power target. The mode-4 maximum does not itself make the GPU
consume 140 W.

Set `hardware.pt516GpuPower.mode = 4` to use mode 4 in the ordinary boot entry.
To keep mode 3 as the default and add a separate mode-4 entry, leave `mode = 3`
and set `hardware.pt516GpuPower.envelopeSpecialisation = true`.

## Rebuild and reboot

From your NixOS configuration directory, build the boot generation. Replace
`HOST` with the name under your flake's `nixosConfigurations`:

```sh
sudo nixos-rebuild boot --flake .#HOST
```

Only after the build succeeds, reboot into the new generation. The driver and
boot image take effect on reboot. A `switch` alone cannot replace the booted
image. When the booted mode differs from the configured mode, the launcher
runs stock powerd until reboot; other identity mismatches refuse startup.

For the optional envelope specialization, use `bootctl list` to find the new
`gpu-envelope` entry and select its exact ID with `sudo bootctl set-oneshot ID`
before rebooting. This selects mode 4 once; the next boot uses the default entry.
Do not run `nixos-rebuild switch` while booted into that specialization.

## Verify

After reboot, inspect the launcher and reported power limits:

```sh
sudo journalctl -b -u nvidia-powerd | grep PT516_GPU_POWER
nvidia-smi -q -d POWER | grep -E 'Max Power Limit|Current Power Limit'
sudo sh -c 'cat /run/pt516-gpu-power/start-*/result.json'
```

The last command expands the wildcard as root because the runtime directory is
private. If it lists multiple service starts, use the result with the greatest
`epoch` and check it against the current boot's journal.

Expect the journal to report `role-8 request returned RM 0x0 / NVML 0` and
`starting powerd with the 125000 mW target rewrite`. The latest result must have
`"prepared": true` and `"target_mw": "125000"`. Its `cap_mw` must be
`"125000"` in mode 3 or `"140000"` in mode 4, matching the reported maximum
of **125 W** or **140 W**. Idle enforced power can be lower than the requested
125 W target. A stock-powerd fallback has null target/cap fields; it does not
confirm this setup is active.

If these checks fail, use the recovery steps below before any runtime writes.
Once they pass, follow the [experiment guide](../tools/README.md) for the idle
seed verdict, identity-write smoke test and optional benchmarks. Its commands
run from this repository's root in a desktop terminal on AC.

## Recovery

- **GPU initialization fails:** inspect the kernel error. If policy preparation
  was refused, boot a known stock generation or add
  `nvidia.NVreg_Pt516BootPolicy=0` at the boot menu to disable editing. Mode 0
  leaves the patched driver installed.
- **Powerd fails or the target is absent:** use the journal and latest startup
  result from [verification](#verify) to find the refused check. Do not bypass
  identity or request checks to force the filter to start.
- **A tool reports a restoration error:** inspect its summary and `restore_check`.
  Fallback may verify only control readback. If restoration fails or remains
  uncertain, reboot to seed 99 A before further experiments.
- **The GPU disappears or reports a fault/reset:** reboot. The retained registry
  entry can also block reinitialization; see [known issues](../CHANGELOG.md#known-issues).

## Revert

Set `hardware.pt516GpuPower.enable = false` or remove the import, rebuild and
reboot. Alternatively, select a generation known to predate this configuration.
An arbitrary earlier generation may still contain the patch.

For temporary filter removal or boot-image disabling, see
[levels of undo](../SAFETY.md#levels-of-undo). Include the relevant logs and
identity in any [problem report](../.github/ISSUE_TEMPLATE/problem.md).
