# Provenance

The public source is `release/v0.1` at [the forge](https://forge.detello.com/nick/gpu-power-policy).
It was exported from a NixOS configuration and a private experiment repository.
Kernel patches apply to NVIDIA open-gpu-kernel-modules
`e394a404108a9ce5bba988c1dddf694dabbd4a43` (595.99.02).

## Historical hardware configuration

- Acer PT516-52s, RTX 3080 Ti Laptop GPU (`10de:2460`, `0000:01:00.0`), VBIOS `94.03.19.00.9F`.
- NixOS, Linux 7.2.7, NVIDIA 595.99.02 open module with GSP; base/tools nixpkgs `ecc58f32d1069a3e3f8a991e2233d365689f748d`; kernel/NVIDIA nixpkgs `58a20b0a882fffd508ca4dad73836d76632792a3` (`linuxPackages_latest`).
- NVML SHA-256: `5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181`.
- Module srcversions: mode 3 `FE193E9810BEB3EED2D3CE9`; mode 4 `CEB9B11F44C43430888F858`.
- Prepared image SHA-256: mode 3 `0501a4c0f0e4c488b1b5d4be0e846330c6fa2de4e4dd610c6d9ee00c1b025d61`; mode 4 `57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73`.
- KDE Plasma 6.6, Wayland/Xwayland; FurMark 2.10.2 Vulkan 2560×1440 on the laptop panel, AC, external cooler.

[Results](docs/results.md) retain historical outputs from October 24, 2026.
[History](docs/history.md) links selected development records and their export methods;
private-source hashes do not independently establish when the work occurred.

## Validation boundary

The hardware measurements predate the portability edits and this cleanup. The
current tree has CPU validation only: no GPU run, kernel build, installation or
reboot was performed for this cut. The reduced collector and launcher, runtime
pin/restore reporting, and opt-in capture setting still need hardware validation.
Other distributions remain untested; see [known issues](CHANGELOG.md#known-issues).

`nix-shell --option max-jobs 1 --option cores 2 --run './lint.sh && ./test.sh'`
passed: lint/format checks, 98 pytest cases and 24 subtests, plus the setter,
capture, boot-image collector, image-edit, identity-guard and C-filter fixtures.
Nix evaluation also checked disabled, mode-3, mode-4 and capture-enabled
configurations against the reference kernel/NVIDIA package set; no derivations were built.
