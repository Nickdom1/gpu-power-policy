# Porting to other distributions (untested)

There is no validated non-NixOS installer. [module/default.nix](../module/default.nix)
and [module/package.nix](../module/package.nix) specify the reference integration;
changing paths or hashes alone does not make another build compatible.

## Requirements

- The reference PT516-52s board and VBIOS. Other images are refused.
- NVIDIA 595.99.02 userspace and open kernel module; Linux 7.2.7 is the tested kernel.
- Kernel build tools/headers, Python 3, GDB and systemd; module signing if Secure Boot requires it.
- KDE Plasma/Xwayland and FurMark for the optional reproduction harness.

## Work needed before any write

1. Apply `policy-shim.patch` to upstream `e394a404108a9ce5bba988c1dddf694dabbd4a43`.
   Mode 4 adds `envelope-mode4.patch`; keep both builds available for recovery.
2. Re-pin the NVML hash, then confirm the hook offsets. Every hook resolves the
   exported symbol `nvmlDeviceSetPowerManagementLimit` and works from the load
   base, so the three internal targets are plain file offsets in NVIDIA's own
   prebuilt library. Your distribution's copy of the same driver version is
   usually that same binary with different packaging metadata, which changes the
   file hash but not the code layout. Check before trusting it:

   ```sh
   sha256sum libnvidia-ml.so.595.99.02        # replace the pins with this
   objdump -T libnvidia-ml.so.595.99.02 | grep -w nvmlDeviceSetPowerManagementLimit
   # the reference build reports 000000000007ca70; a different address means the
   # layout differs and 0xf5b00 / 0xf5be7 / 0xf5bec must be re-derived
   ```

   The pins live in `module/provenance.json`, `module/check-maximum.py`,
   `module/request/request-125.gdb` and `tools/policy-status/codec.py`; the
   kernel/driver/`srcversion` identities are in `tools/policy-125/guard-boot.sh`
   (see [CHANGELOG.md](../CHANGELOG.md#known-issues)).
3. Build the C filter and package the Python runtime, substituting `@nvml@` as
   `package.nix` does. Adapt the module parameters and powerd service from
   `default.nix`, including its environment cleanup, timeouts and artifact directory.
   The launcher takes `--mode 3` or `--mode 4`; capture remains optional and off.
4. Boot with a recoverable stock entry available. Check the prepared-image hash,
   read-only constraints and idle seed before the identity-write smoke test.
   Verify the whole control block reads back unchanged; a successful call alone
   does not establish compatibility or enforcement.

The tools read `/run/opengl-driver/lib/libnvidia-ml.so.1`; symlink that path, or
set `PT516_P13_LIBRARY` for the current-limit client only. The desktop witness
expects KDE Plasma with Xwayland and refuses otherwise, which stops the
reproduction harness but not the boot policy or the launcher. No distribution
copy of NVML has been validated against the shipped offsets.

## What is portable, and what is not

- **Portable in principle:** the kernel patch, the module parameters, the
  launcher, the role-8 request and the powerd target filter. They depend on the
  driver version, not on NixOS. Step 2 is the real work.
- **Pinned to this hardware:** the boot image itself. The patch verifies one
  VBIOS SHA-256 and one PCI identity; on any other board it refuses and, with a
  mode set, the GPU does not initialise that boot.
- **NixOS-shaped:** packaging, the NVML path above and the harness in `tools/`.
  Nothing there is required to use the boot policy.

## Revert

Remove the powerd drop-in and module parameters, reinstall the stock module,
regenerate the initramfs and reboot. See [levels of undo](../SAFETY.md#levels-of-undo).
