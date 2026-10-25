# Safety

This software changes firmware-enforced power and current limits. Readback checks and recovery paths do not establish safe electrical or thermal limits.

## What protects you

- **Nothing is flashed.** The edited power table exists only in RAM for one boot. The original image is verified by SHA-256 first. Booting without the module parameter disables the policy edit.
- **Fail-closed boot.** If the image, PCI identity or checksum does not match, the driver refuses to prepare the policy image and the GPU does not initialise for that boot. Recovery: boot with `nvidia.NVreg_Pt516BootPolicy=0` on the kernel command line, or an earlier generation.
- **The launcher checks before it acts:** driver and kernel version, module `srcversion`, NVML hash, PCI identity, the read-only power constraints, and the kernel log of the boot. A module left over from a previous boot leaves stock `nvidia-powerd` running; any other mismatch fails the service before the target filter starts.
- **Enter the envelope only with `bootctl set-oneshot`.** Entered that way, any reboot returns to the default entry. Nothing enforces this: the `gpu-envelope` specialisation stays in the boot menu and can be selected by hand.
- **Runtime current changes are bounded twice.** The P13 client accepts only 94 000 to the sitting cap (at most the image maximum) in Python, and again inside the GDB hook that performs the write. The write must address policy 13 alone and is built from a fresh read of the control block. The NVML library hash is pinned. Above-maximum board-power requests were refused on this board; above-maximum P13 writes were tested only against a CPU fixture.
- **Each change is read back, and restore is attempted on exit.** After a write the tool reads the control block and the status export and compares them with the request. On exit, including Ctrl-C, errors and workload failure, it attempts to write 99 A back and reads it back the same way. If that guarded write is refused because the control block changed elsewhere, it writes the block read at start as a last resort. That last-resort path checks A61A readback only, not A619 status. The summary distinguishes the checks; if restore failed, P13 may still be raised until reboot, which re-seeds 99 A from the VBIOS.
- **Guards stop the experiment and attempt restoration on:** GPU ≥ 85 °C; any hardware slowdown, thermal or power-brake flag; an Xid in the journal; loss of AC; loss of sustained load; the enforced or maximum power limit differing from what the booted image should give.

## What is not protected

- **VRM temperature.** The regulator's telemetry is not read; GPU temperature does not measure regulator temperature. Sustained operation above stock current was tested only in runs of 2 minutes or less.
- **Faults faster than sampling.** No regulator over-current trip was observed; a fault may remove the GPU from the bus and require a reboot.
- **The estimated current.** P13's "99 A" is the GPU's own estimate, not a measured current.
- **The power input.** A larger adapter does not establish headroom in the laptop's input path.

## Levels of undo

| To undo | Do |
|---|---|
| A running experiment | `Ctrl-C`; the tool attempts the P13 restore and stops the workload |
| A raised P13 after a crash or failed restore | Reboot; the boot image seeds 99 A. Nothing persists |
| The 125 W target (powerd filter and request) | Set `hardware.pt516GpuPower.enable = false`, rebuild, switch and restart `nvidia-powerd` for stock powerd; the boot image stays until reboot |
| The policy image itself | Boot with `nvidia.NVreg_Pt516BootPolicy=0` on the kernel command line, or an earlier generation |

## Tested envelope

| Limit | Tested | Not tested |
|---|---|---|
| P13 (current) | 95–114 A at runtime, as 20–60 s holds inside a 600 s run; 107 A in two 120 s runs (with two 99 A reference runs) | anything longer; anything above 114 A |
| P2 (board power) | 125 W target, in runs up to 10 min | the 140 W image maximum was never used as a target |

Run the tools on AC, with the laptop on a cooler and the GPU otherwise idle. Stop on visual artifacts.
