#!/usr/bin/env bash
# shellcheck disable=SC2154,SC2120
# Offline recovery. Called before package, header, signing, or download checks.
refresh_initramfs() {
    local release
    case $ID in
        cachyos)
            if command -v limine-mkinitcpio >/dev/null; then limine-mkinitcpio
            elif command -v mkinitcpio >/dev/null; then mkinitcpio -P
            elif command -v dracut >/dev/null; then dracut --regenerate-all --force
            else fail 'No initramfs generator found. Reinstall mkinitcpio or dracut before rebooting.'; fi ;;
        fedora)
            for directory in /lib/modules/*; do
                release=${directory##*/}
                [[ ! -e /boot/vmlinuz-$release ]] || dracut --force --kver "$release"
            done ;;
        ubuntu) update-initramfs -u -k all ;;
    esac
}

pin_userspace() {
    python3 - "$@" <<'PY'
from pathlib import Path
import re,shutil,sys
p=Path('/etc/pacman.conf'); text=p.read_text()
start='# BEGIN gpu-power-policy'; end='# END gpu-power-policy'
text=re.sub(r'(?m)^'+start+r'\n.*?^'+end+r'\n?', '', text, flags=re.S)
if sys.argv[1:]:
    block=start+'\nIgnorePkg = '+' '.join(sys.argv[1:])+'\n'+end+'\n'
    text=text.replace('[options]\n', '[options]\n'+block, 1)
    if start not in text: raise SystemExit('pacman.conf has no [options] section')
n=1
while p.with_name(p.name+f'.~{n}~').exists(): n+=1
shutil.copy2(p,p.with_name(p.name+f'.~{n}~'))
p.write_text(text)
PY
}

remove_owned_files() {
    local file
    for file in "$@"; do rm -f -- "$file" "$file".~[0-9]*~; done
}

remove_override_files() {
    remove_owned_files /etc/modprobe.d/gpu-power-policy.conf /etc/dracut.conf.d/gpu-power-policy.conf \
        /etc/initramfs-tools/hooks/gpu-power-policy /etc/mkinitcpio.conf.d/gpu-power-policy.conf
    rm -rf /lib/firmware/nvidia/vbios-override /lib/firmware/nvidia/*/vbios-override
}

disable_project() {
    [[ ! -x /usr/local/bin/gpu-power ]] || /usr/local/bin/gpu-power off || true
    systemctl stop nvidia-powerd.service 2>/dev/null || true
    remove_override_files
    remove_owned_files /etc/systemd/system/nvidia-powerd.service.d/gpu-power-policy.conf \
        /run/gpu-power-policy/target-mw /run/gpu-power-policy/ceiling-mw /run/gpu-power-policy/live.json
    python3 - <<'PY'
import json
from pathlib import Path
p=Path('/etc/gpu-power-policy.json')
if p.exists():
    old=json.loads(p.read_text())
    keep={k:v for k,v in old.items() if k in ('device','installerCheckout')}
    keep['dynamicBoostEnabled']=False
    p.write_text(json.dumps(keep,indent=2)+'\n')

PY
    systemctl daemon-reload
    refresh_initramfs
    echo 'Disabled edited-image loading, boot power targets and the project Dynamic Boost integration; driver and CLI retained. Reboot for stock behavior.'
}

remove_project() {
    disable_project
    if [[ $ID == cachyos ]]; then
        # An empty argument list deliberately removes only our pin.
        # shellcheck disable=SC2119
        pin_userspace
        mapfile -t userspace < <(pacman -Qq nvidia-utils lib32-nvidia-utils 2>/dev/null || true)
        if (( ${#userspace[@]} )); then pacman --noconfirm -R "${userspace[@]}"; fi
    elif command -v nvidia-installer >/dev/null; then
        nvidia-installer --uninstall --silent --no-x-check
    fi
    # Remove recorded modules and our conditional D-Bus copy only while their digests match.
    python3 - <<'PY'
import hashlib
from pathlib import Path
manifest=Path('/var/lib/gpu-power-policy/modules')
if manifest.exists():
    for line in manifest.read_text().splitlines():
        digest,name=line.split('  ',1); p=Path(name)
        if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==digest: p.unlink()
PY
    for directory in /lib/modules/*; do depmod -a "${directory##*/}"; done
    systemctl disable --now gpu-power-policy-init.service 2>/dev/null || true
    remove_owned_files /etc/modprobe.d/gpu-power-policy-nouveau.conf /etc/gpu-power-policy.json \
        /etc/initramfs-tools/hooks/gpu-power-policy-modules \
        /etc/mkinitcpio.conf.d/gpu-power-policy-modules.conf /etc/dracut.conf.d/gpu-power-policy-modules.conf \
        /etc/gpu-power-policy-install.json /etc/systemd/system/gpu-power-policy-init.service \
        /etc/kernel/install.d/40-gpu-power-policy.install \
        /etc/kernel/postinst.d/gpu-power-policy \
        /etc/pacman.d/hooks/85-gpu-power-policy.hook /etc/profile.d/gpu-power-policy.sh /usr/local/bin/gpu-power
    rm -rf /usr/local/lib/gpu-power /var/cache/gpu-power-policy /run/gpu-power-policy
    rm -f /var/lib/gpu-power-policy/modules /var/lib/gpu-power-policy/kernel-notice /var/lib/gpu-power-policy/signing
    systemctl daemon-reload
    refresh_initramfs
    echo "Removed gpu-power-policy and NVIDIA $version. Nouveau is no longer blacklisted by this project."
    case $ID in
        fedora) echo 'Reboot, then install your distribution driver: sudo dnf install akmod-nvidia xorg-x11-drv-nvidia-cuda' ;;
        ubuntu) echo 'Reboot, then install your distribution driver: sudo ubuntu-drivers install' ;;
        cachyos) echo 'Reboot, then install your distribution driver: sudo pacman -Syu nvidia-utils lib32-nvidia-utils linux-cachyos-nvidia-open' ;;
    esac
}
