#!/usr/bin/env bash
# Build the full RM source; the .run/akmods/DKMS prebuilt object cannot take these patches.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=install/lib.sh
source "$repo/install/lib.sh"
[[ $EUID == 0 ]] || fail 'Run with sudo bash install/linux.sh CONFIG.json [--kernel VERSION], or --remove.'
kernel=$(uname -r)
config=
remove=false
while (( $# )); do
    case $1 in
        --kernel) (( $# >= 2 )) || fail '--kernel needs a version'; kernel=$2; shift 2 ;;
        --remove) remove=true; shift ;;
        -*) fail "Unknown option: $1" ;;
        *) [[ -z $config ]] || fail 'Provide only one configuration file'; config=$1; shift ;;
    esac
done
[[ $kernel =~ ^[a-zA-Z0-9._+-]+$ ]] || fail 'Invalid kernel version.'
# shellcheck source=/dev/null
source /etc/os-release
[[ $(uname -m) == x86_64 ]] || fail 'Only x86_64 is supported.'
case $ID:$VERSION_ID in
    fedora:43)
        headers="kernel-devel-$kernel"
        packages=(gcc gcc-c++ make git patch curl python3-pip python3-setuptools "$headers" "kernel-modules-$kernel"
                  elfutils-libelf-devel openssl-devel dracut kmod vulkan-loader)
        install_packages() { dnf -y install "${packages[@]}"; }
        packaged_driver() { rpm -qa --qf '%{NAME}\n' | grep -E '^(akmod-nvidia|kmod-nvidia.*|xorg-x11-drv-nvidia.*|nvidia-driver.*)$'; }
        initramfs() { dracut --force --kver "$1"; }
        image_hook() {
            printf 'install_items+=" /lib/firmware/%s "\n' "$firmware" > "$work/image-hook"
            install -b --backup=numbered -m644 "$work/image-hook" "$dracutconf"
        }
        header_help="sudo dnf install $headers"
        ;;
    ubuntu:24.04)
        headers="linux-headers-$kernel"
        packages=(build-essential git patch curl ca-certificates python3-venv python3-pip python3-setuptools
                  "$headers" "linux-modules-extra-$kernel" libelf-dev libssl-dev initramfs-tools kmod libvulkan1)
        install_packages() { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"; }
        packaged_driver() {
            dpkg -l | awk '$1 == "ii" && $2 ~ /^(nvidia-|libnvidia-|linux-modules-nvidia-|xserver-xorg-video-nvidia)/ {print $2; found=1} END {exit !found}'
        }
        initramfs() { update-initramfs -u -k "$1"; }
        image_hook() {
            cat > "$work/image-hook" <<HOOK
#!/bin/sh
set -e
case "\${1:-}" in prereqs) exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
copy_file firmware /lib/firmware/$firmware
HOOK
            install -b --backup=numbered -m755 "$work/image-hook" "$ubuntuhook"
        }
        header_help="sudo apt-get update && sudo apt-get install $headers"
        ;;
    *) fail "Unsupported release $ID $VERSION_ID; use Fedora 43 or Ubuntu 24.04 LTS." ;;
esac
version=615.71.09
cache=/var/cache/gpu-power-policy
modconf=/etc/modprobe.d/gpu-power-policy.conf
nouveau=/etc/modprobe.d/gpu-power-policy-nouveau.conf
dracutconf=/etc/dracut.conf.d/gpu-power-policy.conf
ubuntuhook=/etc/initramfs-tools/hooks/gpu-power-policy
dropin=/etc/systemd/system/nvidia-powerd.service.d/gpu-power-policy.conf
init=/etc/systemd/system/gpu-power-policy-init.service
saved=/etc/gpu-power-policy-install.json
refresh_all_initrds() {
    local directory release
    for directory in /lib/modules/*; do
        release=${directory##*/}
        [[ ! -e /boot/vmlinuz-$release ]] || initramfs "$release"
    done
}
exec 9>/run/lock/gpu-power-policy.lock
flock -n 9 || fail 'Another installation is running.'
if $remove; then
    if [[ -x /usr/local/bin/gpu-power ]]; then
        /usr/local/bin/gpu-power off || echo 'Some runtime releases failed; reboot after removal.' >&2
    fi
    cleanup_override "$version" "$modconf" "$dracutconf" "$ubuntuhook" "$dropin"
    systemctl disable --now gpu-power-policy-init.service 2>/dev/null || true
    rm -f "$modconf" "$nouveau" "$dracutconf" "$ubuntuhook" "$dropin" "$init" "$saved" \
        /etc/gpu-power-policy.json /usr/local/bin/gpu-power /run/gpu-power-policy/target-mw
    rm -rf /usr/local/lib/gpu-power "/lib/firmware/nvidia/$version/vbios-override" "$cache"
    systemctl daemon-reload
    refresh_all_initrds
    echo 'Project files removed. The installed NVIDIA driver is retained so the system can still boot.'
    echo "To replace it with stock $version:"
    echo "curl -fLO https://download.nvidia.com/XFree86/Linux-x86_64/$version/NVIDIA-Linux-x86_64-$version.run"
    echo "sudo sh NVIDIA-Linux-x86_64-$version.run --kernel-module-type=open --no-dkms"
    echo 'Then regenerate the initramfs and reboot. Secure Boot signing is not provided.'
    exit 0
fi
if names=$(packaged_driver); then
    fail "Packaged NVIDIA driver present ($names). Remove it using your package manager before this installer; no packages were removed."
fi
require_secure_boot_off /sys/firmware/efi
[[ -d /lib/modules/$kernel/build ]] || fail "Headers missing for $kernel. Run: $header_help; then retry --kernel $kernel."
config=${config:-$saved}
[[ -f $config ]] || fail 'Provide CONFIG.json (copy install/example.json); no previous install configuration exists.'
mkdir -p "$cache"
work=$(mktemp -d "$cache/build.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
python3 "$repo/install/config.py" "$config" "$work"
mapfile -t settings < "$work/settings"
bdf=${settings[0]}; rom=${settings[1]}; ceiling=${settings[2]}
install_packages
[[ -d /lib/modules/$kernel/build ]] || fail "Headers still missing. Run: $header_help"
source_dir=$cache/open-gpu-kernel-modules-$version
if [[ ! -d $source_dir ]]; then
    git clone --depth 1 --branch "$version" https://github.com/NVIDIA/open-gpu-kernel-modules.git "$source_dir"
fi
[[ $(git -C "$source_dir" describe --tags --exact-match HEAD) == "$version" ]] || fail 'Cached source has the wrong driver version.'
for name in vbios-override export-boot-vbios; do
    p=$repo/patches/$name-$version.patch
    if patch --dry-run --batch --forward -d "$source_dir" -p1 < "$p" >/dev/null; then
        patch --batch --forward -d "$source_dir" -p1 < "$p"
    else
        patch --dry-run --batch --reverse -d "$source_dir" -p1 < "$p" >/dev/null
    fi
done
if [[ -f $cache/built-kernel && $(cat "$cache/built-kernel") != "$kernel" ]]; then
    make -C "$source_dir/kernel-open" clean SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel"
fi
make -C "$source_dir" -j4 modules SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel"
printf '%s\n' "$kernel" > "$cache/built-kernel"
[[ $(modinfo -F version "$source_dir/kernel-open/nvidia.ko") == "$version" ]]
[[ $(modinfo -F vbios_override_per_gpu "$source_dir/kernel-open/nvidia.ko") == 1 ]]
gcc -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -pthread -o "$work/libpowerd-shim.so" "$repo/src/powerd-shim.c" -ldl
gcc -std=c11 -Wall -Wextra -Werror -O1 -pthread -o "$work/shim-test" "$repo/tests/test_shim.c" -ldl
(cd "$work" && ./shim-test)
python3 "$repo/tests/test_rm.py"
python3 "$repo/tests/test_cli.py"
firmware=nvidia/$version/vbios-override/$bdf.bin
if [[ -n $rom ]]; then
    python3 -I -m venv "$cache/editor"
    "$cache/editor/bin/pip" install 'git+https://github.com/Panchovix/nvidia-vbios-vix-editor.git@edf0f483d8d4a3d7a578f941647c95b503ab9795'
    PATH="$cache/editor/bin:$PATH" python3 "$repo/src/make-image.py" "$rom" "$work/changes.json" "$work/edited.rom" "$work/payload.bin"
    python3 -I - "$work" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); c = json.loads((p/'runtime.json').read_text())
m = json.loads((p/'payload.bin.json').read_text())
c.update(output_size=m['output_size'], output_sha256=m['output_sha256'])
(p/'runtime.json').write_text(json.dumps(c, indent=2) + '\n')
PY
fi
run=$cache/NVIDIA-Linux-x86_64-$version.run
download_driver "$run" "https://download.nvidia.com/XFree86/Linux-x86_64/$version/NVIDIA-Linux-x86_64-$version.run" \
    cdceed22bbeb61248d1a6deabc2596673e3a6501698ee71ac8d2fdc28f3b70fe
mkdir -p /etc/modprobe.d /etc/dracut.conf.d /etc/initramfs-tools/hooks "$(dirname "$dropin")"
printf 'blacklist nouveau\noptions nouveau modeset=0\n' > "$work/nouveau.conf"
install -b --backup=numbered -m644 "$work/nouveau.conf" "$nouveau"
# Userspace only: do not unload nouveau or load patched modules before the required reboot.
sh "$run" --silent --no-kernel-modules --no-dkms --no-nouveau-check
make -C "$source_dir" modules_install SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel" CONFIG_MODULE_SIG_ALL=
depmod -a "$kernel"
[[ $(modinfo -k "$kernel" -F vbios_override_per_gpu nvidia) == 1 ]]
install -Dm755 "$work/libpowerd-shim.so" /usr/local/lib/gpu-power/libpowerd-shim.so
install -Dm644 "$repo/src/rm.py" /usr/local/lib/gpu-power/rm.py
install -Dm644 "$repo/src/policy.py" /usr/local/lib/gpu-power/policy.py
sed 's|^sys.path.insert.*|sys.path.insert(0, "/usr/local/lib/gpu-power")|' "$repo/src/gpu-power.py" > "$work/gpu-power"
install -Dm755 "$work/gpu-power" /usr/local/bin/gpu-power
if [[ -z $rom ]]; then
    if [[ -f /etc/gpu-power-policy.json ]]; then
        /usr/local/bin/gpu-power off || echo 'Some runtime releases failed; reboot to use the stock image.' >&2
    fi
    cleanup_override "$version" "$modconf" "$dracutconf" "$ubuntuhook" "$dropin"
fi
install -b --backup=numbered -m644 "$work/runtime.json" /etc/gpu-power-policy.json
install -b --backup=numbered -m600 "$work/install.json" "$saved"
cat > "$work/init.service" <<'UNIT'
[Unit]
Description=Initialize the selected GPU image's boot power target
After=systemd-modules-load.service
[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=gpu-power-policy
ExecStart=/usr/local/bin/gpu-power boot-init
[Install]
WantedBy=multi-user.target
UNIT
install -b --backup=numbered -m644 "$work/init.service" "$init"
if [[ -n $rom ]]; then
    install -Dm444 "$work/payload.bin" "/lib/firmware/$firmware"
    size=$(stat -c %s "$work/payload.bin")
    echo "options nvidia NVreg_RegistryDwordsPerDevice=\"pci=$bdf;RmLinuxVbiosOverride=2;RmLinuxVbiosOverrideSize=$size\" NVreg_RegistryBinaryFilePerDevice=\"pci=$bdf;RmLinuxVbiosOverrideImage=${firmware#nvidia/}\"" > "$work/modprobe.conf"
    install -b --backup=numbered -m644 "$work/modprobe.conf" "$modconf"
    image_hook
    if command -v nvidia-powerd >/dev/null \
        && powerd_unit=$(systemctl show -p FragmentPath --value nvidia-powerd.service) \
        && [[ -f $powerd_unit ]]; then
        printf '[Unit]\nRequires=gpu-power-policy-init.service\nAfter=gpu-power-policy-init.service\n[Service]\nEnvironment=LD_PRELOAD=/usr/local/lib/gpu-power/libpowerd-shim.so\nEnvironment=GPU_POWER_DEVICE=%s\nEnvironment=GPU_POWER_CEILING_MW=%s\n' "$bdf" "$ceiling" > "$work/powerd.conf"
        install -b --backup=numbered -m644 "$work/powerd.conf" "$dropin"
        install -b --backup=numbered -Dm644 /usr/share/doc/NVIDIA_GLX-1.0/nvidia-dbus.conf /etc/dbus-1/system.d/nvidia-dbus.conf
        systemctl enable nvidia-powerd.service
    fi
fi
systemctl daemon-reload
systemctl enable gpu-power-policy-init.service
systemd-analyze verify "$init"
if [[ -n $rom ]]; then initramfs "$kernel"; else refresh_all_initrds; fi
echo "Installed for $kernel. Reboot into that kernel before running gpu-power."
