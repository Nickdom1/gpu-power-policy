# module

The boot-time policy image and the `nvidia-powerd` launcher. Install guides: [docs/install-nixos.md](../docs/install-nixos.md), [docs/install-other-distros.md](../docs/install-other-distros.md).

| File | Role |
|---|---|
| `policy-shim.patch` | NVIDIA open kernel module (595.99.02, base `e394a404`): retains the driver's boot-time VBIOS extraction, derives a compact in-RAM copy, applies the mode's edit (mode 3: policy 2 maximum 125 000 mW), fixes the checksum, verifies hashes and subimage sums, registers the copy through the registry `vbios` key, and enlarges the GSP bootstrap command queue. With `NVreg_CaptureBootVbios=1` it also exposes the retained raw image read-only at `/proc/driver/nvidia/gpus/<pci>/boot_vbios` |
| `envelope-mode4.patch` | Adds mode 4: policy 2 maximum 140 000 mW and policy 13 maximum 120 000 mA, rated values untouched |
| `launch.py` | Replaces powerd's `ExecStart`: identity checks (kernel, driver, module `srcversion`, NVML hash, PCI), a read-only constraints query, a kernel-log check, one role-8 request, then `exec` of powerd with the filter. A failed identity check stops the service. A boot-policy mode different from the configured mode runs stock powerd until reboot |
| `check-maximum.py`, `inspect-boot-log.py`, `nvml-limit.py` | The launcher's helpers |
| `request/` | The guarded role-8 request: a GDB script that changes one public NVML power-limit call's role byte in the pinned library, and its driver |
| `nvpcf/` | `LD_PRELOAD` filter for powerd that rewrites the Dynamic Boost target it sends (MIT), with its C tests |
| `provenance.json` | The pinned identities the launcher checks |
| `tests/` | CPU-only tests: launcher startup paths, the request hook against a synthetic NVML-shaped library, the image-edit checksum invariant |
| `default.nix`, `package.nix`, `nvpcf/default.nix` | NixOS packaging |

Module parameters: `NVreg_Pt516BootPolicy` (0 off, 1 compact image without an edit, 2 120 W, 3 125 W, 4 envelope) and `NVreg_CaptureBootVbios`.

The boot patch refuses a mismatched image, PCI identity or checksum; the GPU then
fails to initialize for that boot. Disable the edit with
`nvidia.NVreg_Pt516BootPolicy=0`. The launcher separately verifies the running
kernel, driver, module and NVML identities before the role-8 request; it does not
silently skip a failed check. See [SAFETY.md](../SAFETY.md) for recovery.
