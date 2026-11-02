#!/usr/bin/env bash
# Shared installer guards, kept separate so failure paths can be tested without root.
fail() { echo "gpu-power-policy: $*" >&2; exit 1; }

# The config path is optional; production callers use the default.
# shellcheck disable=SC2120
repair_installer() {
    python3 - "${1:-/etc/gpu-power-policy.json}" 2>/dev/null <<'PY_REPAIR' || echo 'sudo bash install/linux.sh'
import json,shlex,sys
from pathlib import Path
p=Path(sys.argv[1]); c=json.loads(p.read_text()) if p.exists() else {}
checkout=Path(c.get('installerCheckout') or '.')
script=checkout/'install/linux.sh'
print('sudo bash '+shlex.quote(str(script) if checkout.is_absolute() and script.is_file() else 'install/linux.sh'))
PY_REPAIR
}

installed_for() {
    [[ $(modinfo -k "$1" -F version nvidia 2>/dev/null) == "${version:?}" &&
       $(modinfo -k "$1" -F vbios_override_per_gpu nvidia 2>/dev/null) == 1 ]]
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

# A repair clears only its own failure; other kernels may still need attention.
clear_kernel_notice() {
    python3 - "$1" "${2:-/var/lib/gpu-power-policy/kernel-notice}" <<'PY_NOTICE'
from pathlib import Path
import sys
kernel,name=sys.argv[1:]; p=Path(name)
if p.exists():
    lines=[line for line in p.read_text().splitlines() if f'the module for {kernel} did not build' not in line]
    if lines: p.write_text('\n'.join(lines)+'\n')
    else: p.unlink()
PY_NOTICE
}

rebuild_installed_kernels() {
    local hook=$1 directory kernel failed=0
    for directory in "${2:-/usr/lib/modules}"/*; do
        [[ -d $directory/build ]] || continue
        kernel=${directory##*/}
        installed_for "$kernel" && continue
        bash "$hook" rebuild "$kernel" || failed=1
    done
    return "$failed"
}

# Rebuild a request from the one runtime configuration; explicit flags win.
saved_flags() {
    python3 - "$@" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); c=json.loads(p.read_text()) if p.exists() else {}
if c.get('device') == sys.argv[2]:
    rom=c.get('romBaseline', {}); stock=rom.get('values', {})
    for key,flag in [('powerCeilingWatts','--power-ceiling'),('currentCeilingMilliamps','--current-ceiling')]:
        if key in stock and c.get(key) != stock[key] and not (rom.get('family') == 'ada' and key.startswith('current')):
            print(flag); print(c[key])
    if c.get('powerdTargetWatts') is not None: print('--boot-power'); print(c['powerdTargetWatts'])
    if c.get('beyondTested'): print('--beyond-tested')
for flag in sys.argv[3:]: print(flag)
PY
}
