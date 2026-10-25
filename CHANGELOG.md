# Changelog

## Unreleased

- Require the NVIDIA renderer at the end of every loaded run; scored holds also
  require positive score/duration fields and duration within 1 s of the request.
- Add CPU CI, compatibility-pin consistency checks and flake linting.
- Correct installation, recovery and validation documentation.

## Initial public snapshot

Boot-time VBIOS policy edits, the powerd target filter, runtime P13 control,
idle/smoke checks and the ladder/ABBA experiments. Reference measurements and
validation scope are in [results](docs/results.md) and [provenance](PROVENANCE.md#validation).

## Known issues

- **AC only.** The installed powerd filter rewrites nonzero targets without checking
  AC state. The experiment runner's AC guard does not disable that persistent filter.
- **GPU reinitialization.** A retained `vbios` registry entry causes the patch to
  refuse a second initialization, including after some reset or driver-close paths.
  Reload the module or reboot. This has not been observed on the reference desktop,
  where the compositor keeps the GPU open.
- **Single-user system.** The collector hands artifact directories to the invoking
  user while root still writes into them; shared or adversarial users are unsupported.
- **Pinned environment.** Other boards, driver/library builds and non-NixOS installs
  are untested. Loaded experiments require KDE Plasma/Xwayland. See the
  [porting requirements](docs/install-other-distros.md).
- **Disabling the edit retains a patched driver.** Mode 0 does not undo driver
  changes such as the registry transport checks. For stock behavior, remove the
  module configuration and rebuild/reboot as described in [recovery](docs/install-nixos.md#revert).
- **Filter loading.** If the filter cannot resolve the original `ioctl`, requests
  fail with `ENOSYS`; it does not fall back to an unfiltered daemon.
