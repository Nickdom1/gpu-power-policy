# Boot policy and launcher

The module changes the board-power policy at boot and starts `nvidia-powerd`
with a 125 W target filter. Installation is covered by the
[NixOS guide](../docs/install-nixos.md) and
[untested porting notes](../docs/install-other-distros.md).

## Boot image

The patch uses the NVIDIA driver's existing VBIOS extraction, makes a compact
copy in RAM, edits the selected policy maximum and fixes the checksum. It
verifies the image hashes and subimage sums before registering the copy through
the driver's `vbios` registry key. It also enlarges the GSP bootstrap command
queue so the image can be handed to firmware. Nothing is flashed.

Mode 3 raises policy 2's board-power maximum to 125 W. Mode 4 raises that
maximum to 140 W and policy 13's current maximum to 120 A. Both retain the
rated values and start current at 99 A; the launcher requests a 125 W target
in either mode. A configured maximum is not measured power consumption.

A mismatched image, PCI identity or checksum prevents GPU initialization for
that boot. `nvidia.NVreg_Pt516BootPolicy=0` disables the edit while retaining the
patched driver. See [recovery](../SAFETY.md#levels-of-undo) for stock-driver undo.
The known [reinitialization issue](../CHANGELOG.md#known-issues) remains.

## Launcher

`launch.py` replaces the power daemon's `ExecStart`. It checks the kernel,
driver, module `srcversion`, NVML library hash and PCI identity, then queries
power constraints and checks the boot log. It makes one guarded role-8 power
request before starting powerd with the target filter. Failed checks stop the
service before the filter starts.

If the booted policy mode differs from the configured mode, the launcher runs
stock powerd until reboot. Other identity mismatches are refused. The filter
rewrites nonzero Dynamic Boost targets without checking AC state, so the
installed configuration is for AC use only.

## Parameters and source files

`NVreg_Pt516BootPolicy` selects 0 (edit disabled), 1 (compact image without an
edit), 2 (120 W), 3 (125 W) or 4 (envelope). The NixOS module exposes modes 3
and 4. `NVreg_CaptureBootVbios=1` additionally retains the original image at
`/proc/driver/nvidia/gpus/<pci>/boot_vbios`, readable only by root. Capture is
off by default; use the local [inspection tools](../tools/boot-vbios/README.md)
when investigating it.

| File | Role |
|---|---|
| `policy-shim.patch` | Boot image preparation, validation and optional capture |
| `envelope-mode4.patch` | Mode-4 board-power and current maxima |
| `launch.py` | Compatibility checks, request and daemon launch |
| `check-maximum.py`, `inspect-boot-log.py`, `nvml-limit.py` | Launcher helpers |
| `request/` | Pinned role-8 NVML request hook and driver |
| `nvpcf/` | Dynamic Boost target filter and C tests |
| `provenance.json` | Pinned identities |
| `tests/` | CPU fixtures for startup, request transport and image edits |
| `default.nix`, `package.nix`, `nvpcf/default.nix` | NixOS packaging |
