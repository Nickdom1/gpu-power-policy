#!/usr/bin/env bash
# Offline module rebuild shared by installation and kernel-update hooks.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$repo/install/lib.sh"
source "$repo/install/pins.sh"
source "$repo/install/sign.sh"
kernel=${1:?kernel version required}
[[ $kernel =~ ^[a-zA-Z0-9._+-]+$ ]] || fail 'Invalid kernel version.'
cache=/var/cache/gpu-power-policy
source_dir=$cache/open-gpu-kernel-modules-$version
[[ -d /lib/modules/$kernel/build ]] || fail "Headers for $kernel are missing. Run: $(repair_installer) --kernel $kernel"
[[ $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || fail 'Cached NVIDIA source commit differs from the pin.'
# Serializes hooks and installer builds without sharing the installer's outer lock.
exec 8>/run/lock/gpu-power-policy-build.lock
flock -x 8
build_args=(SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel")
# CachyOS's default kernel uses Clang ThinLTO; GNU ld cannot link its bitcode.
if grep -qx 'CONFIG_LD_IS_LLD=y' "/lib/modules/$kernel/build/.config"; then
    build_args+=(LLVM=1 LD=ld.lld OBJDUMP=llvm-objdump)
fi
if [[ ! -f $cache/build-kernel || $(cat "$cache/build-kernel") != "$kernel" ]]; then
    make -C "$source_dir/kernel-open" clean SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel"
fi
# Record attempted builds too, so switching kernels after a failed build cleans
# its incompatible objects before retrying with the other kernel's toolchain.
printf '%s\n' "$kernel" > "$cache/build-kernel"
make -C "$source_dir" -j4 modules "${build_args[@]}"
sign_built_modules "$kernel" "$source_dir/kernel-open"
[[ $(modinfo -F version "$source_dir/kernel-open/nvidia.ko") == "$version" ]]
[[ $(modinfo -F vbios_override_per_gpu "$source_dir/kernel-open/nvidia.ko") == 1 ]]
# All builds and signatures succeed before replacing any installed module.
for module in $modules; do
    install -Dm644 "$source_dir/kernel-open/$module.ko" "/lib/modules/$kernel/updates/$module.ko"
    mkdir -p /var/lib/gpu-power-policy
    sha256sum "/lib/modules/$kernel/updates/$module.ko" >> /var/lib/gpu-power-policy/modules
done
depmod -a "$kernel"
clear_kernel_notice "$kernel"
