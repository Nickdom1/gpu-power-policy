# Known gaps

What the current release does not do, in the order it matters to someone installing it.
Each item names the intended fix; none is started unless marked.

Validated configuration: RTX 3080 Ti Laptop GPU (GA103) in an Acer PT516-52s, driver
615.71.09, Linux 7.2.7. Other Ampere, Ada and Turing laptops have an image path but no
end-to-end result here; Blackwell images are refused; desktop cards are unverified. The
limits you set are your responsibility, and the tested 107 A is one board's measurement,
not a recommendation.

1. **Exact driver version.** Everything checks for `615.71.09`, not the 615 series. Policy
   packet sizes and offsets were verified against that build only. Intended fix: a small table
   of verified versions and layouts, plus an option to accept an unverified point release with
   a warning.
2. **Current recovery.** If a current readback mismatches, the prior value is not restored
   automatically. Intended fix: restore the previous current on failure.
3. **Battery.** The boot target applies on battery too. Intended fix: an `onBattery` option
   that passes the stock target through when the AC adapter is offline, identified by supply
   type rather than "any supply offline".
4. **Other distributions.** Fedora 43 and Ubuntu 24.04 share an installer, tested in
   GPU-passthrough VMs (`docs/install.md`). Still missing: bare-metal tests on these
   distributions with Dynamic Boost, module signing for Secure Boot, automatic module
   rebuilds on kernel updates, and an Arch package/initramfs route.
5. **Diagnostics.** `status` prints raw policy tables; the firmware arbitration status (A619)
   is captured by `dump` but not decoded. Intended fix: decode enough of it to name which
   policy is binding, as the research branch's `tools/policy-status/codec.py` did for 595.
6. **Not validated:** sustained operation at the image ceilings, games, suspend and resume,
   battery transitions, multi-GPU systems, any board other than the one in the README;
   desktop card: unverified.
7. **NixOS integration tests.** CI runs the Python and shim CPU suites; `nix flake check` also
   evaluates scratch NixOS configurations, but there is no NixOS VM test yet.
8. **Lost selection log.** `current off` still lowers to rated after the selection log disappears; `limit off` requires selection evidence, so combined `off` can report a limit-step failure.

Deferred release review items (recorded only):

- The CLI is not an importable package; both install paths rewrite it with `sed`.
- The flake builds only `x86_64-linux`.
- Packages lack `description`, `mainProgram` and `platforms` metadata.
- `hardware.gpuPowerPolicy` uses a namespace owned by nixpkgs.
- `hardware.nvidia.package` uses an attrset merge; a later `.overrideAttrs` can lose the patch, and profiles setting the same option can conflict.
- `dump` writes files using the default umask.
- Every failure uses the same exit code.
- Subprocesses have no timeouts, and the CLI lock blocks.
- Boot-VBIOS capture is freed on device teardown, not module unload; the proc node then reports no capture for a GPU that booted.
- Boot-VBIOS allocation uses `GFP_KERNEL` while GPU locks are held.
