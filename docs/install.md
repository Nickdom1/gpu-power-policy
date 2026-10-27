# Install

## NixOS

Prerequisites: `services.xserver.videoDrivers = [ "nvidia" ]`, `nixpkgs.config.allowUnfree = true`, and a
`boot.kernelPackages` whose `nvidiaPackages.latest` is exactly 615.71.09. nixpkgs
`58a20b0a882fffd508ca4dad73836d76632792a3` provides that with Linux 7.2.7. Do not set
`hardware.nvidia.package`; the module sets it to the patched open driver. x86_64-linux only.
It defaults `hardware.nvidia.open` to true and rejects an explicit false value.

On Dynamic Boost laptops, also enable `hardware.nvidia.dynamicBoost.enable`. Other
machines use a direct limit request for the boot target; desktop cards are unverified.

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

## Fedora 43 and Ubuntu 24.04 LTS

Debian-family derivatives use the Ubuntu path unchanged; only the package manager,
headers package and initramfs tool differ between the two families, and both are handled.

Use x86_64, driver 615.71.09, Secure Boot off, no packaged NVIDIA driver, and a text
console with the graphical session stopped. Install headers for the target kernel first:
`sudo dnf install kernel-devel-$(uname -r)` on Fedora, or
`sudo apt-get install linux-headers-$(uname -r)` on Ubuntu. Keep a way back to the
previous working driver. The installer refuses unsupported releases, missing headers,
packaged NVIDIA drivers and enabled Secure Boot; it never removes packaged drivers.
If the repositories no longer carry headers for the running kernel, install the current
kernel and matching headers, reboot into it, then run the installer.

The patches change NVIDIA's resource manager, which the `.run` kernel-module build and
akmods ship as a prebuilt object. `install/linux.sh` builds the full
[open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/615.71.09)
source, installs matching userspace with `--no-kernel-modules`, and uses the editor
pinned to `edf0f483`. It blacklists Nouveau and regenerates the initramfs. Reboot is
required before the patched module can load.

```sh
cp install/example.json gpu-power.json    # "vbios": null for bootstrap
sudo bash install/linux.sh gpu-power.json && sudo reboot
sudo gpu-power rom /var/lib/gpu.rom
```

Set the ROM and limits, then rerun the installer and reboot:

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

Relative ROM paths are relative to the config file. A null device selects the only NVIDIA
GPU; set its PCI address on multi-GPU systems. These limits belong to the tested laptop;
read your own ROM's stock values. Verify image selection with `sudo gpu-power status` and
limits with `nvidia-smi -q -d POWER`. Without powerd, `target` reports that the daemon is
not running and leaves a numeric target unchanged; use `gpu-power limit` instead.

Installed files: the payload under `/lib/firmware/nvidia/615.71.09/vbios-override/`,
`/etc/modprobe.d/gpu-power-policy.conf`, `/etc/modprobe.d/gpu-power-policy-nouveau.conf`,
`/usr/local/bin/gpu-power`, `/usr/local/lib/gpu-power/`, `/etc/gpu-power-policy.json`, and
`/etc/gpu-power-policy-install.json` (the saved config for `--kernel`). Fedora includes the
payload through `/etc/dracut.conf.d/gpu-power-policy.conf`; Ubuntu uses
`/etc/initramfs-tools/hooks/gpu-power-policy`. The init unit is always installed under
`/etc/systemd/system/`; the powerd drop-in is installed only when its binary and unit exist.
Every overwritten file under `/etc` gets a numbered backup (`.~1~`, `.~2~`, etc.).
Both `vbios: null` and `--remove` disable and stop powerd, remove its project drop-in and
D-Bus policy, delete the override payload, module options and initramfs hook, and refresh
all installed initramfs images. Bootstrap retains the CLI, init unit, stock config and
Nouveau blacklist for ROM export. Removal also deletes those files; the installed driver
and numbered backups remain.

## Tested configurations

| Target | What was verified |
|---|---|
| NixOS, Acer PT516-52s, RTX 3080 Ti Laptop, 615.71.09, Linux 7.2.7 | Everything, natively: image staging, ROM export, powerd target, current control, the five-run load comparison |
| Fedora 43 VM, GPU passthrough, kernel 7.2.12 (fresh), 7.2.11 (existing-disk regression) | Install, repeat install, ROM export, image staging, current control, limit request at boot and at runtime, removal; headless FurMark results below |
| Ubuntu 24.04 VM, GPU passthrough, kernel 6.8.0-147-generic | Same as Fedora; kernel update untested because the image already has the newest available GA kernel |

The powerd target needs the laptop's Dynamic Boost firmware and is verified natively
only. Machines without it, including VMs, get the boot target through `gpu-power limit`.
Desktop cards: the image edit, ROM export, `current` and `limit` apply; none of it has
been run on a desktop card.

Headless FurMark 2.10.2 Vulkan at 2560×1440, a 30 s warm-up then one 30 s run per phase.
All phases used the edited 140 W image ceiling; stock/restored means runtime 80 W / 99 A.
Power is the mean of 1 Hz samples during each benchmark; peak temperature was 67 °C.
These short VM runs do not establish sustained performance or physical HDMI output.

| Phase | Fedora: mean power / score | Ubuntu: mean power / score |
|---|---:|---:|
| Stock limit, 99 A | 79.88 W / 1308 | 79.86 W / 1337 |
| Limit 125 W, 99 A | 114.38 W / 2241 | 114.24 W / 2243 |
| Limit 125 W, 107 A | 123.35 W / 2265 | 123.33 W / 2254 |
| `off`, restored 80 W / 99 A | 79.89 W / 1279 | 79.89 W / 1280 |

[Ubuntu evidence](evidence/2026-10-27-ubuntu24-vm.json) and
[Fedora evidence](evidence/2026-10-27-fedora43-vm.json) include the no-Dynamic-Boost
smoke tests. `nvidia-smi -pl 125` prints "not supported" but exits zero on this GPU;
the CLI uses NVML's actual status and falls back to role 8. Both guests enforced
125 W after two boots, and `limit off` restored 80 W.

## What off restores, and how to go back further

`off` restores the rated current, removes the powerd target override and sends the
stock limit. The edited image stays until reboot. To boot stock: set `vbios` to null
(NixOS: `enable = false`), rebuild or reinstall, reboot. To remove the project on Fedora
or Ubuntu: `sudo bash install/linux.sh --remove`, reinstall a stock driver, reboot.
Removal deletes the project's Nouveau blacklist; without the stock-driver reinstall,
Nouveau can bind the GPU on the next boot and NVIDIA tools will be unavailable.

## Updates

Kernel update: run `install/linux.sh --kernel <new version>` after the kernel and its
headers are installed and before rebooting into it; the previous kernel keeps working.
NixOS rebuilds the module with the system. Driver update: unsupported until a new pinned
version is verified; keep 615.71.09.

---

The install path, image staging, ROM export, current control and limit request were verified
on Fedora 43 and Ubuntu 24.04 with this laptop's GPU passed through to a VM: 79.9 W and 1308
points at stock against 123.4 W and 2265 with both limits raised. A VM exposes no Dynamic
Boost firmware, so the `nvidia-powerd` path is verified on the laptop only; everywhere else
the boot target falls back to a direct limit request.
