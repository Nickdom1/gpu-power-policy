#!/usr/bin/env bash
# Shared installer guards, kept separate so failure paths can be tested without root.
fail() { echo "gpu-power-policy: $*" >&2; exit 1; }

require_secure_boot_off() {
    local efi=$1 secureboot value found=false
    [[ -d $efi ]] || return 0 # Legacy BIOS has no UEFI Secure Boot state.
    for secureboot in "$efi"/efivars/SecureBoot-*; do
        [[ -e $secureboot ]] || continue
        found=true
        value=$(od -An -tu1 -j4 -N1 "$secureboot") || fail "Cannot read Secure Boot state from $secureboot. Mount efivarfs and retry."
        [[ $value =~ ^[[:space:]]*[01][[:space:]]*$ ]] || fail "Invalid or empty Secure Boot state in $secureboot; refusing unsigned modules."
        [[ $value -eq 0 ]] || fail 'Secure Boot is enabled. Disable it in firmware; this installer does not sign modules.'
    done
    $found || fail 'UEFI Secure Boot state is unavailable. Mount efivarfs or use firmware exposing SecureBoot; refusing unsigned modules.'
}

download_driver() {
    local run=$1 url=$2 digest=$3 candidate
    candidate=$run
    if [[ ! -f $run ]]; then
        candidate=$run.tmp
        curl -fL "$url" -o "$candidate" || { rm -f "$candidate"; fail 'Driver download failed; rerun the installer to retry.'; }
    fi
    if ! printf '%s  %s\n' "$digest" "$candidate" | sha256sum -c -; then
        rm -f "$candidate"
        fail "Driver checksum mismatch; removed $candidate. Rerun the installer to download it again."
    fi
    [[ $candidate == "$run" ]] || mv "$candidate" "$run"
}

cleanup_override() {
    local version=$1 modconf=$2 dracutconf=$3 ubuntuhook=$4 dropin=$5
    # Stop powerd before removing its shim, including upgrades from installers without state tracking.
    systemctl disable --now nvidia-powerd.service 2>/dev/null || true
    rm -f "$modconf" "$dracutconf" "$ubuntuhook" "$dropin" \
        /etc/dbus-1/system.d/nvidia-dbus.conf /run/gpu-power-policy/target-mw
    rm -rf "/lib/firmware/nvidia/$version/vbios-override"
}
