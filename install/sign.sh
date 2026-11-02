#!/usr/bin/env bash
# shellcheck disable=SC2154
# Sourced after preflight has decided whether lockdown requires module signatures.
mok_enrolled() {
    # mokutil 0.6 uses zero for "not enrolled" and one for "already enrolled".
    # Other versions differ; require positive output instead of trusting the exit code.
    local result
    result=$(LC_ALL=C mokutil --test-key "$1" 2>/dev/null) || true
    [[ $result == "$1 is already enrolled" ]]
}

prepare_signing() {
    [[ $sign_modules == true ]] || return 0
    local key cert
    if ! command -v mokutil >/dev/null || ! command -v openssl >/dev/null ||
       { [[ $ID == ubuntu ]] && ! command -v update-secureboot-policy >/dev/null; }; then
        case $ID in
            fedora) dnf -y install mokutil openssl ;;
            ubuntu) apt-get update; apt-get install -y mokutil shim-signed openssl ;;
            *) fail 'Kernel lockdown requires a supported signing path; see docs/install.md#secure-boot.' ;;
        esac
    fi
    case $ID in
        ubuntu)
            key=/var/lib/shim-signed/mok/MOK.priv
            cert=/var/lib/shim-signed/mok/MOK.der
            if [[ ! -f $key || ! -f $cert ]]; then update-secureboot-policy --new-key; fi
            ;;
        fedora)
            key=/etc/pki/akmods/private/private_key.priv
            cert=/etc/pki/akmods/certs/public_key.der
            if [[ ! -f $key || ! -f $cert ]]; then
                key=/var/lib/gpu-power-policy/mok/MOK.priv
                cert=/var/lib/gpu-power-policy/mok/MOK.der
                if [[ ! -f $key || ! -f $cert ]]; then
                    install -d -m700 "$(dirname "$key")"
                    (umask 077; openssl req -new -x509 -newkey rsa:3072 -keyout "$key" -outform DER \
                        -out "$cert" -nodes -days 3650 -subj '/CN=gpu-power-policy module signing/')
                fi
            fi
            ;;
        *) fail 'Kernel lockdown requires a supported signing path; see docs/install.md#secure-boot.' ;;
    esac
    [[ -f $key && -f $cert ]] || fail 'MOK key creation failed; no module was installed.'
    if ! mok_enrolled "$cert"; then
        cat >&3 <<'MESSAGE'
Secure Boot / kernel lockdown requires signing. Modules will be signed with a Machine Owner Key.
Choose a one-time password now. At the next boot a blue "Perform MOK management" screen
appears: choose Enroll MOK, Continue, Yes, type the password, Reboot.
MESSAGE
        # Ubuntu's --enroll-key helper exits without importing when no DKMS modules
        # exist. This project builds modules directly, so import the distro MOK here.
        mokutil --timeout -1
        mokutil --import "$cert" >&3 2>&3
    fi
    install -d -m755 /var/lib/gpu-power-policy
    printf '%s\n%s\n' "$key" "$cert" > /var/lib/gpu-power-policy/signing
    chmod 600 /var/lib/gpu-power-policy/signing
}

sign_built_modules() {
    local kernel=$1 directory=$2 key cert module
    [[ -f /var/lib/gpu-power-policy/signing ]] || return 0
    { read -r key; read -r cert; } < /var/lib/gpu-power-policy/signing
    [[ -f $key && -f $cert ]] || fail 'Saved signing key is unavailable; restore it before building a new kernel.'
    for module in $modules; do
        "/lib/modules/$kernel/build/scripts/sign-file" sha256 "$key" "$cert" "$directory/$module.ko"
    done
}
