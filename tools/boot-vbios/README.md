# Boot-VBIOS inspection

Copy the VBIOS image retained by the patched driver at boot, then decode its
power-policy table offline. The collector reads the retained copy; it does not
access PCI ROM or issue a GPU request. This optional research tool is not needed
for installation, smoke tests, holds or the ladder.

On NixOS, set `hardware.pt516GpuPower.captureBootVbios = true`, rebuild and
reboot as described in the [installation guide](../../docs/install-nixos.md).
This retains the original image (about 600 KB) behind a root-only proc node.
Capture is disabled by default, and the tools remain pinned to the reference
board and driver.

From the repository root, choose an output directory that does not already exist:

```sh
sudo python3 tools/boot-vbios/collect-boot-vbios.py --output-dir /var/tmp/vbios
```

Expect `"complete_read": true` and `"state": "captured"` in `capture.json`,
plus `snapshot.bin` and `flash-copy.bin`. The directory is private to root,
so use root to read it for offline inspection as well:

```sh
sudo python3 tools/boot-vbios/inspect-saved-vbios.py /var/tmp/vbios
```

The decoder prints JSON containing the image checks and power-policy table.
It accepts the observed version-3 layout with a 44-byte header and 71-byte
entries. If the collector refuses, inspect `capture.json` and check the capture
setting and running identity. If the decoder refuses, retain the files and
investigate the reported mismatch; do not bypass the checks or treat a partial
capture as a valid image. Keep captured images private; they are firmware.
