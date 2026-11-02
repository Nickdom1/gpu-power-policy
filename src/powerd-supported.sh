#!/bin/sh
# A stock install cannot query NVIDIA before the first reboot.
# systemd skips the vendor daemon on GPUs without Dynamic Boost support.
device=$(cat /run/gpu-power-policy/device)
exec grep -qE '^Notebook Dynamic Boost:[[:space:]]+Supported[[:space:]]*$' \
    "/proc/driver/nvidia/gpus/$device/power"
