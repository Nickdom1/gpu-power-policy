# boot-vbios

Read the VBIOS image the patched driver retained at boot, and decode its power-policy table offline. No ROM access and no GPU request. This is optional developer tooling, unused by install, smoke, hold and ladder checks.

On NixOS, set `hardware.pt516GpuPower.captureBootVbios = true`, rebuild and reboot
to retain the original image (about 600 KB) behind a root-only proc node. Capture
is disabled by default. The collector and decoder remain pinned to the reference
board; they do not enable support for another GPU.

```sh
sudo python3 collect-boot-vbios.py --output-dir /var/tmp/vbios   # copies /proc/driver/nvidia/gpus/0000:01:00.0/boot_vbios (needs NVreg_CaptureBootVbios=1)
python3 inspect-saved-vbios.py /var/tmp/vbios                   # decodes the BIT 'P' power-policy table: index, type, min/rated/max per policy
```

The decoder accepts only the table layout seen on this board (version 3, 44-byte header, 71-byte entries). On another board it reports what it cannot parse instead of guessing. Keep captured images private; they are firmware.
