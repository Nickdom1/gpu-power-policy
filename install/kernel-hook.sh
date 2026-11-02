#!/usr/bin/env bash
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
repo=/usr/local/lib/gpu-power/installer
source "$repo/install/lib.sh"
source "$repo/install/pins.sh"
# shellcheck source=/dev/null
source /etc/os-release
# Rescan after kernel or header upgrades, and on initial installation.
if [[ ${1:-} == all ]]; then
    rebuild_installed_kernels "$0"
    exit $?
fi
[[ ${1:-} != remove ]] || exit 0
kernel=${2:-${1:-}}
[[ $kernel =~ ^[a-zA-Z0-9._+-]+$ ]] || exit 1
previous=$(uname -r)
if bash "$repo/install/rebuild-module.sh" "$kernel"; then
    exit 0
fi
mkdir -p /var/lib/gpu-power-policy
if [[ $ID == cachyos ]]; then
    message="gpu-power-policy: the module for $kernel did not build; your NVIDIA GPU will be off at the next boot. Boot linux-cachyos-lts if installed, or run: $(repair_installer) --kernel $kernel"
else
    message="gpu-power-policy: the module for $kernel did not build; your boot menu still has $previous. Run: $(repair_installer) --kernel $kernel"
fi
clear_kernel_notice "$kernel"
printf '%s\n' "$message" | tee -a /var/lib/gpu-power-policy/kernel-notice >&2
exit 1
