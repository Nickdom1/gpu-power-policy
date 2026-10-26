# Known gaps

What the current release does not do, in the order it matters to someone installing it.
Each item names the intended fix; none is started unless marked.

1. **Exact driver version.** Everything checks for `615.71.09`, not the 615 series. Policy
   packet sizes and offsets were verified against that build only. Intended fix: a small table
   of verified versions and layouts, plus an option to accept an unverified point release with
   a warning.
2. **No powerd, no target.** The target override acts through `nvidia-powerd`'s Dynamic Boost
   request. Desktop cards and VMs have no such request, so the board-power target cannot be
   raised there. Intended fix: a `gpu-power limit <W>` command through the firmware's
   set-limit control (the request this project used before the powerd path replaced it).
3. **Failure recovery is reported, not transactional.** If the powerd restart after `target`
   fails, the new target file stays in place; if a current readback mismatches, the prior value
   is not restored automatically. Intended fix: restore the previous state on either failure.
4. **Boot target skips the staging check.** The CLI refuses `target` unless the kernel log shows
   the edited image was staged; the boot-time initialiser writes the configured target without
   that check. Intended fix: the same check in the shim or the initialiser, with a test.
5. **Battery.** The boot target applies on battery too. Intended fix: an `onBattery` option
   that passes the stock target through when the AC adapter is offline, identified by supply
   type rather than "any supply offline".
6. **Other distributions.** No install path for the patched module, firmware file, powerd
   drop-in and config outside NixOS. The CLI's booted-module check reads a NixOS path.
   In progress: a Fedora install script, tested in a VM with the GPU passed through.
7. **Diagnostics.** `status` prints raw policy tables; the firmware arbitration status (A619)
   is captured by `dump` but not decoded. Intended fix: decode enough of it to name which
   policy is binding.
8. **Not validated:** sustained operation at the image ceilings, games, suspend and resume,
   battery transitions, multi-GPU systems, any board other than the one in the README.
9. **Tests and CI.** CPU tests exist; no CI workflow and no NixOS VM test yet. Intended fix: a
    workflow running `nix flake check`, which is CPU-only by design.
