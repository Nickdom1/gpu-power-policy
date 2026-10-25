# Safety

Use this configuration on AC, under supervision, with the laptop on a cooler.
It changes firmware-enforced power and current limits. The installed powerd
filter rewrites nonzero targets without checking AC state; the experiment
runner's AC guard does not disable that persistent filter.

Readback checks and recovery paths do not establish safe electrical or thermal
limits. Keep the GPU otherwise idle before starting an experiment, and stop on
visual artifacts.

## Boot and runtime checks

The power table is edited only in RAM for one boot. The driver verifies the
original image by SHA-256 and refuses a mismatched image, PCI identity or
checksum. That refusal prevents GPU initialization for the boot. Disable the
edit with `nvidia.NVreg_Pt516BootPolicy=0`, or boot a known stock generation.
Mode 0 still uses a patched driver.

The launcher checks the driver, kernel, module `srcversion`, NVML hash, PCI
identity, read-only power constraints and boot log before requesting the target.
A different booted policy mode runs stock powerd until reboot; other identity
mismatches stop the service before the filter starts.

Runtime writes are bounded in both Python and the GDB hook: 94 000 mA through
the configured run cap, no higher than the image maximum. Each write addresses
policy 13 alone and starts from a fresh control-block read. The NVML hash is
pinned. Above-maximum board-power requests were refused on this board;
above-maximum current writes were tested only against a CPU fixture.

During loaded experiments, guards stop the run and attempt restoration for GPU
temperature at or above 85 °C, hardware slowdown, thermal or power-brake flags,
an Xid driver error, loss of AC or sustained load, and unexpected enforced or
maximum power limits.

## Runtime restoration

After each write, the tool compares the control readback (A61A) and firmware
status (A619) with the requested value. Normal restoration writes 99 A and
performs both checks. Errors, workload failure and `Ctrl-C` also trigger a
restoration attempt when the runner may have changed the limit.

If normal restoration throws an exception, the fallback attempts to write the
block saved at setup. It checks **A61A control readback only**, not A619 firmware
status. This fallback can follow any normal-restore exception, including a
status-check failure; it is not full restoration verification.

The saved `result.json` distinguishes `a61a+a619`, `a61a-only` and `failed`.
Read its `errors` as well as `restore_check`. If restoration fails or remains
uncertain, stop and reboot before further experiments; the boot image seeds
99 A again.

## Limits of the checks

- **Regulator temperature is not read.** GPU temperature does not measure it.
  Sustained operation above stock current was tested only in runs of 2 minutes
  or less.
- **Sampling can miss fast faults.** No regulator over-current trip was observed;
  a fault may remove the GPU from the bus and require a reboot.
- **Firmware current is an estimate.** P13's reported amperes are not calibrated
  physical-current measurements.
- **The input path is not validated by adapter size.** A larger adapter does not
  establish headroom in the laptop's power input.

## Levels of undo

| To undo | Action and consequence |
|---|---|
| A running experiment | Press `Ctrl-C`; the tool stops the workload and attempts the current restore |
| Raised current after a crash or uncertain restore | Reboot to seed 99 A; the configured boot mode and powerd filter remain |
| The persistent 125 W target filter | Set `hardware.pt516GpuPower.enable = false`, rebuild, switch and restart `nvidia-powerd` for stock powerd; the boot image stays until reboot |
| The boot policy edit for one boot | Add `nvidia.NVreg_Pt516BootPolicy=0` at the boot menu; this retains the patched driver |
| The patched driver and configuration | Disable the module, rebuild and reboot, or boot a generation known to predate this configuration |

Setting `mode = 4` uses it on every boot. The optional `gpu-envelope`
specialization can instead be selected once with `bootctl set-oneshot`; the
next boot uses the default entry. Rebooting resets runtime current, not the
configured mode or installed filter. See the
[NixOS recovery guide](docs/install-nixos.md#recovery) or
[porting undo steps](docs/install-other-distros.md#revert).

## Historical test conditions

| Limit | Tested | Not tested |
|---|---|---|
| P13 (current) | 95–114 A as 20–60 second holds inside a 600 second run; 107 A in two 120 second runs with two 99 A references | Longer holds or settings above 114 A |
| P2 (board power) | 125 W target in runs up to 10 minutes | The 140 W image maximum as a target |

See [results](docs/results.md) for measurements and
[validation](PROVENANCE.md#validation) for hardware and automated checks.
