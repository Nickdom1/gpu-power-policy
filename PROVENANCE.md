# Provenance

Kernel patches apply to NVIDIA open-gpu-kernel-modules
`e394a404108a9ce5bba988c1dddf694dabbd4a43` (595.99.02).

## Historical hardware configuration

- Acer PT516-52s, RTX 3080 Ti Laptop GPU (`10de:2460`, `0000:01:00.0`), VBIOS `94.03.19.00.9F`.
- NixOS, Linux 7.2.7, NVIDIA 595.99.02 open module with GSP; base/tools nixpkgs `ecc58f32d1069a3e3f8a991e2233d365689f748d`; kernel/NVIDIA nixpkgs `58a20b0a882fffd508ca4dad73836d76632792a3` (`linuxPackages_latest`).
- NVML SHA-256: `5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181`.
- Module srcversions: mode 3 `FE193E9810BEB3EED2D3CE9`; mode 4 `CEB9B11F44C43430888F858`.
- Prepared image SHA-256: mode 3 `0501a4c0f0e4c488b1b5d4be0e846330c6fa2de4e4dd610c6d9ee00c1b025d61`; mode 4 `57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73`.
- KDE Plasma 6.6, Wayland/Xwayland; FurMark 2.10.2 Vulkan 2560×1440 on the laptop panel, AC, external cooler.

[Results](docs/results.md) contains the October 24, 2026 measurements.
[History](docs/history.md) describes the investigation and its source records.

## Validation

- **Hardware:** the reference laptop produced the published benchmarks on
  October 24, 2026.
- **Builds:** mode-3 and mode-4 drivers and the NixOS mode-4 configuration built
  successfully on October 25 from `dcfaa60`. Driver srcversions are listed above.
- **Automated tests:** startup guards, control and restoration, image editing,
  and benchmark validation passed checks that run without a GPU.

Installation, reboot checks and benchmarks have not been repeated with the
current tools.

Run the [local checks](README.md#install-and-check) or view
[CI results](https://github.com/Nickdom1/gpu-power-policy/actions/workflows/checks.yml).
