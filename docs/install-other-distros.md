# Porting to other distributions (untested)

There is no validated non-NixOS installer. The kernel patch, launcher and powerd
filter are Linux components; packaging and compatibility checks need adapting.
The boot image remains pinned to the PT516-52s board and VBIOS.

## Requirements

- NVIDIA 595.99.02 userspace and open kernel module; Linux 7.2.7 is the reference kernel.
- Kernel build tools/headers, Python 3, GDB and systemd; module signing if required.
- KDE Plasma/Xwayland and FurMark for the reproduction harness.

## Porting work

1. Apply `module/policy-shim.patch` to upstream
   `e394a404108a9ce5bba988c1dddf694dabbd4a43`; mode 4 adds `envelope-mode4.patch`.
2. Validate the NVML binary before changing its hash pin. The hooks derive a load
   base from the exported NVML setter or constraints getter, then use internal offsets,
   register conventions and structure layouts. A matching exported symbol address
   alone does **not** validate those internal assumptions. Inspect every hook in
   `module/request/request-125.gdb`, `tools/current-limit/client.gdb` and
   `tools/policy-status/capture.gdb` against the target binary. After validation,
   calculate its hash for the pin:

   ```sh
   sha256sum /path/to/validated/libnvidia-ml.so.1
   ```
3. Update identities consistently: `module/provenance.json`, `module/check-maximum.py`,
   `module/request/request-125.gdb`, `tools/policy-status/codec.py` and
   `tools/policy-125/guard-boot.sh`. CPU pin-consistency tests detect disagreement;
   they do not establish compatibility with a new library.
4. Package the runtime and filter as `module/package.nix` and `module/nvpcf/default.nix`
   do. Adapt `module/default.nix`'s parameters and service, including environment
   cleanup and timeouts. The launcher takes `--mode 3` or `--mode 4`.
5. With a stock boot entry available, verify image preparation, read-only
   constraints and idle seeds, then the identity-write smoke test and restoration.
   A successful request alone does not establish enforcement.

Tools default to `/run/opengl-driver/lib/libnvidia-ml.so.1`; adapt that path for
all readers. `PT516_P13_LIBRARY` overrides it for the current-limit client only.
The desktop witness requires KDE Plasma/Xwayland. No non-NixOS NVML build has
been validated against these hooks.

## Revert

Remove the powerd drop-in and module parameters, reinstall the stock module,
regenerate the initramfs and reboot. See [recovery](../SAFETY.md#levels-of-undo).
