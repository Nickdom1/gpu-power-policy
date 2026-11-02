#!/usr/bin/env bash
# Build NVIDIA's full RM source; never load/unload the running graphics driver here.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$repo/install/lib.sh"
source "$repo/install/pins.sh"
source "$repo/install/sign.sh"
source "$repo/install/lifecycle.sh"
[[ $EUID == 0 ]] || fail 'Run: sudo bash install/linux.sh'
kernel=$(uname -r)
device='' mode=install
configure_flags=()
while (( $# )); do
    case $1 in
        --kernel|--device)
            (( $# >= 2 )) || fail "$1 needs a value"
            if [[ $1 == --kernel ]]; then kernel=$2; else device=$2; fi
            shift 2 ;;
        --power-ceiling|--current-ceiling|--boot-power)
            (( $# >= 2 )) || fail "$1 needs a value"
            configure_flags+=("$1" "$2"); shift 2 ;;
        --beyond-tested) configure_flags+=("$1"); shift ;;
        --disable|--remove) [[ $mode == install ]] || fail 'Choose --disable or --remove'; mode=${1#--}; shift ;;
        *) fail "Unknown option: $1" ;;
    esac
done
[[ $kernel =~ ^[a-zA-Z0-9._+-]+$ ]] || fail 'Invalid kernel version.'
[[ -z $device || $device =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]$ ]] || fail 'Invalid device BDF.'
# shellcheck source=/dev/null
source /etc/os-release
if [[ $mode != install ]]; then
    case $ID in fedora|ubuntu|cachyos) ;; *) fail 'Recovery supports Fedora, Ubuntu and CachyOS.' ;; esac
    [[ ${#configure_flags[@]} == 0 ]] || fail 'Recovery flags cannot be combined with configuration.'
    exec 9>/run/lock/gpu-power-policy.lock
    flock -n 9 || fail 'Another installation is running.'
    if [[ $mode == disable ]]; then disable_project; else remove_project; fi
    exit 0
fi
case $ID:${VERSION_ID:-} in fedora:43|ubuntu:24.04|cachyos:*) ;; *) fail 'Use Fedora 43, Ubuntu 24.04 or CachyOS.' ;; esac
# No package changes, cache writes, driver writes, or boot-file changes before these refusals.
preflight_args=(--json --kernel "$kernel")
[[ -z $device ]] || preflight_args+=(--device "$device")
if ! preflight=$(python3 "$repo/install/preflight.py" "${preflight_args[@]}"); then
    python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<< "$preflight"
    exit 1
fi
mapfile -t preflight_values < <(python3 -c 'import json,sys
c=json.load(sys.stdin)
for k in ("device","sign","pkgbase"): print(str(c[k]).lower() if type(c[k]) is bool else c[k])' <<< "$preflight")
device=${preflight_values[0]}; sign_modules=${preflight_values[1]}; pkgbase=${preflight_values[2]}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
flags=$(saved_flags /etc/gpu-power-policy.json "$device" "${configure_flags[@]}")
if [[ -n $flags ]]; then mapfile -t configure_flags <<< "$flags"; fi
if (( ${#configure_flags[@]} )); then
    [[ -f /proc/driver/nvidia/gpus/$device/boot_vbios ]] || fail 'Reboot into the stock install first, then rerun this command.'
    PATH="/usr/local/lib/gpu-power/bin:$PATH" python3 "$repo/src/gpu-power.py" --device "$device" configure "${configure_flags[@]}" > "$work/request.json"
else
    printf '{"device":"%s","vbios":null,"powerdTargetWatts":null}\n' "$device" > "$work/request.json"
fi
rom=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["vbios"] or "")' "$work/request.json")
cp "$work/request.json" "$work/runtime.json"
bdf=$device
exec 9>/run/lock/gpu-power-policy.lock
flock -n 9 || fail 'Another installation is running.'
log=/var/log/gpu-power-policy/install.log
install -d -m755 "$(dirname "$log")"
exec 3>&1 >>"$log" 2>&1
trap 'result=$?; rm -rf -- "$work"; if (( result )); then
    printf "Installation failed. Log: %s\n" "$log" >&3
    tail -n 30 "$log" >&3
fi' EXIT
phase() {
    printf '%s\n' "$*" >&3
    printf '%s\n' "$*"
}
phase 'Preparing the signing key, if required.'
prepare_signing
cache=/var/cache/gpu-power-policy
mkdir -p "$cache"
source_dir=$cache/open-gpu-kernel-modules-$version
installed=false
if installed_for "$kernel"; then installed=true; fi
# A module can already be present after a userspace download/install failed.
# Only skip the complete driver phase once that phase finished successfully.
if $installed && [[ ! -f $cache/userspace-version || $(cat "$cache/userspace-version") != "$version" ]]; then
    installed=false
fi
if [[ $sign_modules == true ]] && $installed; then
    [[ -f /var/lib/gpu-power-policy/signing ]] || installed=false
    for module in $modules; do
        [[ -n $(modinfo -k "$kernel" -F signer "$module" 2>/dev/null || true) ]] || installed=false
    done
fi
if ! $installed; then
    phase 'Installing build tools and kernel headers.'
    case $ID in
        fedora) dnf -y install gcc gcc-c++ make git patch curl \
            "kernel-modules-$kernel" "kernel-devel-$kernel" nvidia-gpu-firmware elfutils-libelf-devel openssl-devel dracut kmod \
            vulkan-loader ;;
        ubuntu) apt-get update
            kernel_modules=linux-modules-$kernel
            if apt-cache show "linux-modules-extra-$kernel" >/dev/null 2>&1; then
                kernel_modules=linux-modules-extra-$kernel
            fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential git patch curl ca-certificates \
                "$kernel_modules" "linux-headers-$kernel" \
                libelf-dev libssl-dev initramfs-tools kmod libvulkan1 ;;
        cachyos) pacman --noconfirm -S --needed base-devel git patch curl python kmod clang llvm lld -- "$pkgbase-headers" ;;
    esac
    phase 'Fetching and patching the NVIDIA source.'
    if [[ ! -d $source_dir ]]; then
        git init "$source_dir"
        git -C "$source_dir" remote add origin https://github.com/NVIDIA/open-gpu-kernel-modules.git
        git -C "$source_dir" fetch --depth 1 origin "$source_commit"
        git -C "$source_dir" checkout --detach FETCH_HEAD
    fi
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || fail 'Cached NVIDIA source has the wrong commit.'
    while read -r name; do
        p=$repo/patches/$name
        if patch --dry-run --batch --forward -d "$source_dir" -p1 < "$p" >/dev/null; then
            patch --batch --forward -d "$source_dir" -p1 < "$p"
        else patch --dry-run --batch --reverse -d "$source_dir" -p1 < "$p" >/dev/null; fi
    done < "$repo/patches/series"
    phase 'Building the NVIDIA driver.'
    bash "$repo/install/rebuild-module.sh" "$kernel" || fail "$version did not build against $kernel. On CachyOS, install linux-cachyos-lts and its headers, boot it, and retry."
    phase 'Installing NVIDIA userspace.'
    if [[ $ID == cachyos ]]; then
        archive_packages=(nvidia-utils)
        if grep -Eq '^\[multilib\]' /etc/pacman.conf; then archive_packages+=(lib32-nvidia-utils); fi
        files=()
        for package in "${archive_packages[@]}"; do
            if [[ $package == nvidia-utils ]]; then pkgrel=$nvidia_utils_pkgrel; else pkgrel=$lib32_nvidia_utils_pkgrel; fi
            file="$cache/$package-$version-$pkgrel-x86_64.pkg.tar.zst"
            url="https://archive.archlinux.org/packages/${package:0:1}/$package/${file##*/}"
            if [[ $package == nvidia-utils ]]; then digest=$nvidia_utils_sha256; else digest=$lib32_nvidia_utils_sha256; fi
            download_driver "$file" "$url" "$digest"
            curl -fL "$url.sig" -o "$file.sig"
            pacman-key --verify "$file.sig" "$file"
            files+=("$file")
        done
        pacman --noconfirm -U "${files[@]}"
        pin_userspace "${archive_packages[@]}"
        echo "Installed ${files[*]##*/} from archive.archlinux.org and pinned in /etc/pacman.conf (IgnorePkg)."
        phase 'pacman will report the pinned NVIDIA userspace packages as ignored during upgrades.'
    else
        run=$cache/NVIDIA-Linux-x86_64-$version.run
        download_driver "$run" "https://download.nvidia.com/XFree86/Linux-x86_64/$version/${run##*/}" "$driver_sha256"
        sh "$run" --silent --no-kernel-modules --no-dkms --no-nouveau-check --no-x-check
    fi
    printf '%s\n' "$version" > "$cache/userspace-version"
else
    phase "Patched $version module is already installed for $kernel; skipping driver build and userspace installation."
fi
phase 'Installing the CLI, editor and boot configuration.'
# The pinned editor's source launcher needs only the Python standard library.
editor=/usr/local/lib/gpu-power/editor
if [[ ! -d $editor/.git ]]; then git init "$editor"; fi
if [[ $(git -C "$editor" rev-parse HEAD 2>/dev/null || true) != "$editor_commit" ]]; then
    git -C "$editor" fetch --depth 1 https://github.com/Panchovix/nvidia-vbios-vix-editor.git "$editor_commit"
    git -C "$editor" checkout --detach FETCH_HEAD
fi
printf '#!/bin/sh\nexec python3 -I /usr/local/lib/gpu-power/editor/nvbios "$@"\n' > "$work/nvbios"
install -Dm755 "$work/nvbios" /usr/local/lib/gpu-power/bin/nvbios
gcc -DGPU_POWER_DRIVER_VERSION="\"$version\"" -std=c11 -Wall -Wextra -O2 -fPIC -shared -pthread -o "$work/libpowerd-shim.so" "$repo/src/powerd-shim.c" -ldl
firmware=nvidia/vbios-override/$bdf.bin
if [[ -n $rom ]]; then
    PATH="/usr/local/lib/gpu-power/bin:$PATH" python3 "$repo/src/make-image.py" "$rom" "$work/runtime.json" "$work"
fi
# Install runtime tools and the offline kernel-update implementation.
install -Dm755 "$work/libpowerd-shim.so" /usr/local/lib/gpu-power/libpowerd-shim.so
install -Dm755 "$repo/src/powerd-supported.sh" /usr/local/lib/gpu-power/powerd-supported
for source in rm policy baseline; do install -Dm644 "$repo/src/$source.py" "/usr/local/lib/gpu-power/$source.py"; done
sed 's|^sys.path.insert.*|sys.path.insert(0, "/usr/local/lib/gpu-power")\nos.environ["PATH"] = "/usr/local/lib/gpu-power/bin:" + os.environ["PATH"]|' "$repo/src/gpu-power.py" > "$work/gpu-power"
install -Dm755 "$work/gpu-power" /usr/local/bin/gpu-power
mkdir -p /usr/local/lib/gpu-power/installer/install
cp "$repo"/install/*.sh /usr/local/lib/gpu-power/installer/install/
install -Dm755 "$repo/install/kernel-hook.sh" /usr/local/lib/gpu-power/installer/install/kernel-hook.sh
# The installed wrapper expands its own argument.
# shellcheck disable=SC2016
case $ID in
    fedora)
        # Build before 50-dracut.install snapshots modules for the new kernel.
        # Remove the previous hook name on upgrades (compatibility for one release).
        rm -f /etc/kernel/install.d/60-gpu-power-policy.install
        install -Dm755 "$repo/install/kernel-hook.sh" /etc/kernel/install.d/40-gpu-power-policy.install ;;
    ubuntu) printf '#!/bin/sh\nexec /usr/local/lib/gpu-power/installer/install/kernel-hook.sh rebuild "$1"\n' > "$work/postinst"
        # Build before initramfs-tools snapshots modules for the new kernel.
        rm -f /etc/kernel/postinst.d/zz-gpu-power-policy
        install -Dm755 "$work/postinst" /etc/kernel/postinst.d/gpu-power-policy ;;
    cachyos) cat > "$work/pacman-hook" <<'HOOK'
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = linux-cachyos*
[Action]
Description = Rebuilding the gpu-power-policy module before initramfs generation
When = PostTransaction
Exec = /usr/local/lib/gpu-power/installer/install/kernel-hook.sh all
HOOK
        install -Dm644 "$work/pacman-hook" /etc/pacman.d/hooks/85-gpu-power-policy.hook ;;
esac
if [[ $ID == cachyos ]]; then
    phase 'Building drivers for other installed kernels with headers.'
    rebuild_installed_kernels /usr/local/lib/gpu-power/installer/install/kernel-hook.sh
fi
printf 'blacklist nouveau\noptions nouveau modeset=0\n' > "$work/nouveau.conf"
install -Dm644 "$work/nouveau.conf" /etc/modprobe.d/gpu-power-policy-nouveau.conf
# Native image-generator configuration includes every module at stock and raised.
case $ID in
    ubuntu)
        # shellcheck disable=SC2016
        printf '#!/bin/sh\ncase "${1:-}" in prereqs) exit 0 ;; esac\n. /usr/share/initramfs-tools/hook-functions\nmanual_add_modules %s\n' "$modules" > "$work/modules-hook"
        install -Dm755 "$work/modules-hook" /etc/initramfs-tools/hooks/gpu-power-policy-modules ;;
    *)
        if [[ $ID == cachyos ]] && command -v mkinitcpio >/dev/null; then
            printf 'MODULES+=(%s)\n' "$modules" > "$work/modules-hook"
            install -Dm644 "$work/modules-hook" /etc/mkinitcpio.conf.d/gpu-power-policy-modules.conf
        else
            printf 'add_drivers+=" %s "\n' "$modules" > "$work/modules-hook"
            install -Dm644 "$work/modules-hook" /etc/dracut.conf.d/gpu-power-policy-modules.conf
        fi ;;
esac
if [[ -n $rom ]]; then
    install -Dm444 "$work/payload.bin" "/lib/firmware/$firmware"
    install -Dm644 "$work/modprobe.conf" /etc/modprobe.d/gpu-power-policy.conf
    if [[ $ID == ubuntu ]]; then
        cat > "$work/image-hook" <<HOOK
#!/bin/sh
case "\${1:-}" in prereqs) exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
copy_file firmware /lib/firmware/$firmware
HOOK
        install -Dm755 "$work/image-hook" /etc/initramfs-tools/hooks/gpu-power-policy
    elif [[ $ID == cachyos ]] && command -v mkinitcpio >/dev/null; then
        printf 'FILES+=(/usr/lib/firmware/%s)\n' "$firmware" > "$work/image-hook"
        install -Dm644 "$work/image-hook" /etc/mkinitcpio.conf.d/gpu-power-policy.conf
    else
        printf 'install_items+=" /lib/firmware/%s /etc/modprobe.d/gpu-power-policy.conf /etc/modprobe.d/gpu-power-policy-nouveau.conf "\n' "$firmware" > "$work/image-hook"
        install -Dm644 "$work/image-hook" /etc/dracut.conf.d/gpu-power-policy.conf
    fi
else
    # Returning to stock does not disable the vendor daemon.
    remove_override_files
fi
cat > "$work/init.service" <<'UNIT'
[Unit]
Description=Read GPU firmware bounds and initialize the optional boot target
After=systemd-modules-load.service
Before=nvidia-powerd.service
[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=gpu-power-policy
ExecStart=/usr/local/bin/gpu-power boot-init
[Install]
WantedBy=multi-user.target
UNIT
install -Dm644 "$work/init.service" /etc/systemd/system/gpu-power-policy-init.service
# The same shim is present at stock; it forwards untouched unless boot-init supplies a ceiling and target.
# Query support without loading the NVIDIA module during installation.
if command -v nvidia-powerd >/dev/null && systemctl cat nvidia-powerd.service >/dev/null 2>&1; then
    printf '[Unit]\nRequires=gpu-power-policy-init.service\nAfter=gpu-power-policy-init.service\n[Service]\nExecCondition=/usr/local/lib/gpu-power/powerd-supported\nEnvironment=LD_PRELOAD=/usr/local/lib/gpu-power/libpowerd-shim.so\n' > "$work/powerd.conf"
    install -Dm644 "$work/powerd.conf" /etc/systemd/system/nvidia-powerd.service.d/gpu-power-policy.conf
    if [[ -f /usr/share/doc/NVIDIA_GLX-1.0/nvidia-dbus.conf ]]; then
        if [[ ! -f /etc/dbus-1/system.d/nvidia-dbus.conf ]]; then
            install -Dm644 /usr/share/doc/NVIDIA_GLX-1.0/nvidia-dbus.conf /etc/dbus-1/system.d/nvidia-dbus.conf
            install -d -m755 /var/lib/gpu-power-policy
            sha256sum /etc/dbus-1/system.d/nvidia-dbus.conf >> /var/lib/gpu-power-policy/modules
        fi
    fi
    systemctl enable nvidia-powerd.service
fi
cat > "$work/notice.sh" <<'SHNOTICE'
for notice in /run/gpu-power-policy/notice /var/lib/gpu-power-policy/kernel-notice; do
    if [ -r "$notice" ]; then cat "$notice"; fi
done
SHNOTICE
install -Dm644 "$work/notice.sh" /etc/profile.d/gpu-power-policy.sh
# Runtime and saved configuration are committed last, after all image/driver work succeeds.
python3 - "$work/runtime.json" "$repo" <<'PY_CONFIG'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); c=json.loads(p.read_text()); c['installerCheckout']=sys.argv[2]
p.write_text(json.dumps(c,indent=2)+'\n')
PY_CONFIG
install -m644 "$work/runtime.json" /etc/gpu-power-policy.json
systemctl daemon-reload
systemctl enable gpu-power-policy-init.service
phase 'Updating boot files.'
refresh_initramfs
exec >&3 2>&3
echo "Installed NVIDIA $version with VBIOS override support for $bdf, kernel $kernel."
echo 'Reboot, then run: sudo gpu-power status'
if [[ -n $rom ]]; then
    echo 'If the display does not come back, follow https://github.com/Nickdom1/gpu-power-policy/blob/main/docs/install.md#recovery'
    echo "Then run: $(repair_installer) --disable"
fi
