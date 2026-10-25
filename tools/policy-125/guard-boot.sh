# shellcheck shell=bash
# Sourced by the root-run tools. Reads identity files; makes no GPU request.
if (( EUID != 0 )); then
    printf 'Run this script with sudo bash.\n' >&2; exit 1
fi
[[ $(uname -r) == 7.2.7 ]] || { echo 'STOP: kernel differs'; exit 1; }
[[ $(cat /sys/module/nvidia/version) == 595.99.02 ]] || { echo 'STOP: driver differs'; exit 1; }
[[ $(cat /sys/bus/pci/devices/0000:01:00.0/vendor) == 0x10de && $(cat /sys/bus/pci/devices/0000:01:00.0/device) == 0x2460 ]] || { echo 'STOP: PCI identity differs'; exit 1; }
policy_mode=$(cat /sys/module/nvidia/parameters/NVreg_Pt516BootPolicy)
module_srcversion=$(cat /sys/module/nvidia/srcversion)
# Mode 3: policy 2 max 125 W. Mode 4: policy 2 max 140 W, policy 13 max 120 A.
# Both modes preserve the rated values; either can be the normal boot entry.
# Each mode is bound to its own module; PT516_ALLOW_MODE4=1 opts a caller in.
case $policy_mode in
  3) [[ $module_srcversion == FE193E9810BEB3EED2D3CE9 ]] || { echo 'STOP: experimental module differs'; exit 1; }
     policy_p2_max_mw=125000; policy_p13_max_ma=99000 ;;
  4) [[ ${PT516_ALLOW_MODE4:-0} == 1 ]] || { echo 'STOP: envelope mode 4 boot; this tool is pinned to mode 3'; exit 1; }
     [[ $module_srcversion == CEB9B11F44C43430888F858 ]] || { echo 'STOP: envelope module differs'; exit 1; }
     policy_p2_max_mw=140000; policy_p13_max_ma=120000 ;;
  *) echo 'STOP: 125 W experiment mode 3 (or envelope mode 4) is required'; exit 1 ;;
esac
export policy_mode policy_p2_max_mw policy_p13_max_ma
