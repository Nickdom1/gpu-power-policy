# Install

## NixOS

Prerequisites: `hardware.nvidia.open = true`, `hardware.nvidia.dynamicBoost.enable = true`,
`services.xserver.videoDrivers = [ "nvidia" ]`, `nixpkgs.config.allowUnfree = true`, and a
`boot.kernelPackages` whose `nvidiaPackages.latest` is exactly 615.71.09. nixpkgs
`58a20b0a882fffd508ca4dad73836d76632792a3` provides that with Linux 7.2.7. Do not set
`hardware.nvidia.package`; the module sets it to the patched open driver. x86_64-linux only.

Two rebuilds: the first exports your ROM, the second applies the edited image.

**1. Bootstrap.** Import the module and enable it with only your GPU's PCI address. The GPU
behaves stock; the patched driver just keeps a root-only copy of the ROM it read at boot.

```nix
# flake.nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/58a20b0a882fffd508ca4dad73836d76632792a3"; # 615.71.09
  inputs.gpu-power-policy.url = "github:Nickdom1/gpu-power-policy";
  outputs = { nixpkgs, gpu-power-policy, ... }: {
    nixosConfigurations.HOST = nixpkgs.lib.nixosSystem {
      modules = [ gpu-power-policy.nixosModules.default ./configuration.nix ];
    };
  };
}

# configuration.nix
hardware.gpuPowerPolicy = {
  enable = true;
  device = "0000:01:00.0";   # the VGA/3D controller line of `lspci -D | grep -i nvidia`, lowercase
  vbios = null;              # bootstrap: export only
};
```

Rebuild, reboot, then export and inspect:

```
sudo gpu-power rom /var/lib/gpu.rom                       # 0600, never overwrites; prints size and sha256
sudo nix shell github:Nickdom1/gpu-power-policy#vix-editor -c nvbios inspect /var/lib/gpu.rom
```

An Ampere laptop image is around 600 KB. Keep the file out of any public repository: it will
also be copied into the world-readable Nix store, so reference it by a hash-pinned local URL
rather than a path inside your flake. Rebuild with `sudo nixos-rebuild` so evaluation can read
the root-only file.

**2. Configure.** Set the ROM, your ceilings and the boot target, then rebuild and reboot.

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

**3. Verify.**

```
sudo gpu-power status | grep -A2 '"image"'    # expect "state": "selected"
nvidia-smi -q -d POWER                         # expect your ceiling as the maximum
```

If the firmware rejects the image, the driver logs it and boots the stock VBIOS; `status`
reports `rejected` and the CLI refuses to raise anything.

The numbers above are the PT516-52s's. Read your ROM's stock default and rated current from
`nvbios inspect` and keep ceilings close to them. Ceilings and active settings are separate on
purpose. The image above permits 140 W and 120 A; at boot the target is 125 W and the current stays at 99 A until you raise it. Nothing
applies 107 A automatically. Rollback: boot the previous generation, or set `enable = false`,
rebuild and reboot. That removes the patched driver, the firmware file and the module
parameters, and `hardware.nvidia.package` returns to your configuration's default.

Already have a ROM, or cannot reboot twice? Any complete image works: the sysfs `rom`
attribute (`echo 1 > .../rom; cat rom; echo 0 > .../rom`; EIO or a short file on some
laptops), `nvflash --save` on Linux, or GPU-Z on Windows. Validate it with `nvbios inspect`
as above. The export was hardware-validated on 2026-10-26
([record](evidence/2026-10-26-boot-vbios-export.json)).

## Fedora 43

Same two rounds as NixOS, driven by a JSON config with the same fields. Tested in a Fedora 43
VM with the GPU passed through, kernel 7.2.11, driver 615.71.09
([record](evidence/2026-10-26-fedora43-vm.json)); the powerd target acts only on a real
laptop, since a VM exposes no Dynamic Boost firmware.

The patches change NVIDIA's resource manager, which the `.run` kernel-module build and akmods
ship as a prebuilt object. The installer therefore builds the full
[open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/615.71.09)
source for the running kernel, installs matching userspace from the `.run` with
`--no-kernel-modules`, and uses the editor as a Python package pinned to `edf0f483`.

Requirements: Fedora 43 x86_64, development files for the running kernel, Secure Boot off (the
installer does not sign modules), no packaged NVIDIA driver installed, a text console with the
graphical session stopped, and a way back to your previous driver. The module is built for the
running kernel only; rerun the installer after a kernel update.

```sh
cp install/fedora.json fedora.json       # "vbios": null for the first round
sudo bash install/fedora.sh fedora.json && sudo reboot
sudo gpu-power rom /var/lib/gpu.rom
```

Then set `vbios` and your limits (relative paths are relative to the config file; `device`
null picks the only NVIDIA GPU, set it on multi-GPU systems), rerun the installer, reboot, and
verify as for NixOS:

```json
{
  "device": null,
  "vbios": "/var/lib/gpu.rom",
  "powerCeilingWatts": 140,
  "powerDefaultWatts": 80,
  "currentCeilingMilliamps": 120000,
  "currentRatedMilliamps": 99000,
  "powerdTargetWatts": 125
}
```

Installed files: the payload under `/lib/firmware/nvidia/615.71.09/vbios-override/`, with a
dracut include in `/etc/dracut.conf.d/` and the modprobe options in `/etc/modprobe.d/`, both
named `gpu-power-policy.conf`; the CLI in `/usr/local/bin`, its modules and the shim in
`/usr/local/lib/gpu-power`; `/etc/gpu-power-policy.json`; the powerd drop-in and the
`gpu-power-policy-init` unit under `/etc/systemd/system/`; NVIDIA's D-Bus policy in
`/etc/dbus-1/system.d/`.

Rollback: set `vbios` and `powerdTargetWatts` to null, rerun, reboot; that removes the
override and the units and keeps the ROM export. To remove the patched driver, reinstall the
stock one, `depmod -a`, `dracut --force`, reboot.
