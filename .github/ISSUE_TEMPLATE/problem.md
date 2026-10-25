---
name: Problem
about: Something refused, failed or behaved unexpectedly on the reference board
title: ""
labels: ""
---

<!-- Other boards are unsupported; use the board-report template. For initialization failures, include the kernel error rather than assuming its cause. -->

<!-- Before posting logs or paths, remove hostnames, usernames, serial numbers
     and GPU/boot UUIDs. Keep driver versions, limits and error messages. -->

**Commit** (`git rev-parse HEAD`):

**Kernel, driver, module** (`uname -r`, `cat /sys/module/nvidia/version`, `modinfo -F srcversion nvidia`):

**Laptop model and VBIOS** (`nvidia-smi -q | grep -i -E 'product name|vbios'`):

**Exact command and its printed summary**, including the `Saved:` path:

**`journalctl -b -u nvidia-powerd`** output:

**If a tool reported `RESTORATION` or `LAST-RESORT`:** did you reboot, and what does `nvidia-smi -q -d POWER` show now?

<!-- Do not attach captured VBIOS images. -->
