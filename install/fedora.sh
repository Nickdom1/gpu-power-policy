#!/usr/bin/env bash
# Full RM source is required: DKMS/akmods' prebuilt RM object cannot take these patches.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID == 0 && $# == 1 ]] || { echo "Usage: sudo bash install/fedora.sh CONFIG.json" >&2; exit 1; }
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == fedora && $VERSION_ID == 43 ]] || { echo 'This installer requires Fedora 43' >&2; exit 1; }
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=615.71.09
kernel=$(uname -r)
cache=/var/cache/gpu-power-policy
mkdir -p "$cache"
exec 9>"$cache/install.lock"
flock -n 9 || { echo 'Another installation is running' >&2; exit 1; }
work=$(mktemp -d "$cache/build.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
python3 - "$1" "$work" <<'PY'
import json, pathlib, re, sys
def require(ok, message):
    if not ok: sys.exit('fedora.sh: ' + message)
config_path, work = map(pathlib.Path, sys.argv[1:])
c = json.loads(config_path.read_text())
keys = ['powerCeilingWatts', 'powerDefaultWatts', 'currentCeilingMilliamps', 'currentRatedMilliamps']
require(isinstance(c, dict) and not (c.keys() - set(keys + ['device', 'vbios', 'powerdTargetWatts'])), 'unknown configuration key')
gpus = [p.name for p in pathlib.Path('/sys/bus/pci/devices').iterdir()
        if (p/'vendor').read_text().strip() == '0x10de' and int((p/'class').read_text(), 16) >> 16 == 3]
bdf = c.get('device')
if bdf is None:
    require(len(gpus) == 1, 'set device explicitly when there is not exactly one NVIDIA GPU')
    bdf = gpus[0]
require(isinstance(bdf, str) and re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]', bdf) and bdf in gpus, 'invalid NVIDIA GPU device')
rom = c.get('vbios')
if rom is not None:
    require(isinstance(rom, str) and rom and '\n' not in rom and '\r' not in rom, 'invalid vbios path')
    rom = str((config_path.resolve().parent / rom).resolve(strict=True))
    require('\n' not in rom and '\r' not in rom, 'invalid resolved vbios path')
    require(all(type(c.get(k)) is int and 1 <= c[k] <= 4294967 for k in keys), 'configured mode needs four positive limits')
    require(c[keys[1]] <= c[keys[0]] and c[keys[3]] <= c[keys[2]], 'defaults must not exceed ceilings')
t = c.get('powerdTargetWatts')
require(t is None or (rom and type(t) is int and 1 <= t <= c[keys[0]]), 'invalid boot powerd target')
runtime = {'device': bdf, 'modinfo': '/usr/sbin/modinfo', 'systemctl': '/usr/bin/systemctl', 'journalctl': '/usr/bin/journalctl', 'dmesg': '/usr/bin/dmesg', 'nvidiaSmi': '/usr/bin/nvidia-smi'}
if rom:
    runtime.update({k: c[k] for k in keys})
    (work/'changes.json').write_text(json.dumps(dict(zip(['power_max_w', 'power_default_w', 'nvvdd_output_ocp_max_ma', 'nvvdd_output_ocp_rated_ma'], [c[k] for k in keys]))))
(work/'runtime.json').write_text(json.dumps(runtime, indent=2) + '\n')
(work/'settings').write_text('\n'.join([bdf, rom or '', str(c[keys[0]] * 1000) if rom else '', str(t * 1000) if t else '']) + '\n')
PY
mapfile -t settings < "$work/settings"
bdf=${settings[0]}; rom=${settings[1]}; ceiling=${settings[2]}; target=${settings[3]}
dnf -y install gcc gcc-c++ make git patch curl python3-pip python3-setuptools \
    "kernel-devel-$kernel" elfutils-libelf-devel openssl-devel dracut kmod vulkan-loader
[[ -d /lib/modules/$kernel/build ]] || { echo "Missing headers for $kernel" >&2; exit 1; }
source_dir=$cache/open-gpu-kernel-modules-$version
if [[ ! -d $source_dir ]]; then
    git clone --depth 1 --branch "$version" https://github.com/NVIDIA/open-gpu-kernel-modules.git "$source_dir"
fi
[[ $(git -C "$source_dir" describe --tags --exact-match HEAD) == "$version" ]]
for name in vbios-override export-boot-vbios; do
    p=$repo/patches/$name-$version.patch
    if patch --dry-run --batch --forward -d "$source_dir" -p1 < "$p" >/dev/null; then
        patch --batch --forward -d "$source_dir" -p1 < "$p"
    else
        patch --dry-run --batch --reverse -d "$source_dir" -p1 < "$p" >/dev/null
    fi
done
make -C "$source_dir" -j4 modules SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel"
[[ $(modinfo -F version "$source_dir/kernel-open/nvidia.ko") == "$version" ]]
[[ $(modinfo -F vbios_override_per_gpu "$source_dir/kernel-open/nvidia.ko") == 1 ]]
gcc -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -o "$work/libpowerd-shim.so" "$repo/src/powerd-shim.c" -ldl
gcc -std=c11 -Wall -Wextra -Werror -O1 -DSHIM_TEST -o "$work/shim-test" "$repo/src/powerd-shim.c"
"$work/shim-test"
python3 "$repo/tests/test_rm.py"
python3 "$repo/tests/test_cli.py"
firmware=nvidia/$version/vbios-override/$bdf.bin
if [[ -n $rom ]]; then
    python3 -m venv "$cache/editor"
    "$cache/editor/bin/pip" install 'git+https://github.com/Panchovix/nvidia-vbios-vix-editor.git@edf0f483d8d4a3d7a578f941647c95b503ab9795'
    PATH="$cache/editor/bin:$PATH" python3 "$repo/src/make-image.py" "$rom" "$work/changes.json" "$work/edited.rom" "$work/payload.bin"
    python3 - "$work" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); c = json.loads((p/'runtime.json').read_text())
m = json.loads((p/'payload.bin.json').read_text())
c.update(output_size=m['output_size'], output_sha256=m['output_sha256'])
(p/'runtime.json').write_text(json.dumps(c, indent=2) + '\n')
PY
fi
mkdir -p "$work/plan"
if [[ -n $rom ]]; then
    size=$(stat -c %s "$work/payload.bin")
    echo "options nvidia NVreg_RegistryDwordsPerDevice=\"pci=$bdf;RmLinuxVbiosOverride=2;RmLinuxVbiosOverrideSize=$size\" NVreg_RegistryBinaryFilePerDevice=\"pci=$bdf;RmLinuxVbiosOverrideImage=${firmware#nvidia/}\"" > "$work/plan/modprobe.conf"
    echo "install_items+=\" /lib/firmware/$firmware \"" > "$work/plan/dracut.conf"
    printf '[Unit]\nRequires=gpu-power-policy-init.service\nAfter=gpu-power-policy-init.service\n[Service]\nEnvironment=LD_PRELOAD=/usr/local/lib/gpu-power/libpowerd-shim.so\nEnvironment=GPU_POWER_DEVICE=%s\nEnvironment=GPU_POWER_CEILING_MW=%s\n' "$bdf" "$ceiling" > "$work/plan/powerd.conf"
    printf '[Unit]\nDescription=Initialize boot GPU powerd target\nBefore=nvidia-powerd.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\nRuntimeDirectory=gpu-power-policy\n' > "$work/plan/gpu-power-policy-init.service"
    if [[ -n $target ]]; then
        echo "ExecStart=/bin/sh -c 'echo $target > /run/gpu-power-policy/target-mw'" >> "$work/plan/gpu-power-policy-init.service"
    else
        echo 'ExecStart=/usr/bin/rm -f /run/gpu-power-policy/target-mw' >> "$work/plan/gpu-power-policy-init.service"
    fi
    systemd-analyze verify "$work/plan/gpu-power-policy-init.service"
fi
for secureboot in /sys/firmware/efi/efivars/SecureBoot-*; do
    [[ -e $secureboot ]] || continue
    [[ $(od -An -tu1 -j4 -N1 "$secureboot") -eq 0 ]] || {
        echo 'This installer does not sign modules; Secure Boot must be disabled.' >&2; exit 1;
    }
done
if rpm -qa --qf '%{NAME}\n' | grep -E '^(akmod-nvidia|kmod-nvidia.*|xorg-x11-drv-nvidia.*|nvidia-driver.*)$' >/dev/null; then
    echo 'Remove the packaged NVIDIA driver before using the .run installation route.' >&2; exit 1
fi
run=$cache/NVIDIA-Linux-x86_64-$version.run
if [[ ! -f $run ]]; then
    curl -fL "https://download.nvidia.com/XFree86/Linux-x86_64/$version/NVIDIA-Linux-x86_64-$version.run" -o "$run.tmp"
    mv "$run.tmp" "$run"
fi
echo "cdceed22bbeb61248d1a6deabc2596673e3a6501698ee71ac8d2fdc28f3b70fe  $run" | sha256sum -c -
sh "$run" --silent --no-kernel-modules --no-dkms
make -C "$source_dir" modules_install SYSSRC="/lib/modules/$kernel/build" KERNEL_UNAME="$kernel" CONFIG_MODULE_SIG_ALL=
depmod -a "$kernel"
[[ $(modinfo -k "$kernel" -F vbios_override_per_gpu nvidia) == 1 ]]
install -Dm755 "$work/libpowerd-shim.so" /usr/local/lib/gpu-power/libpowerd-shim.so
install -Dm644 "$repo/src/rm.py" /usr/local/lib/gpu-power/rm.py
install -Dm644 "$repo/src/policy.py" /usr/local/lib/gpu-power/policy.py
sed 's|^sys.path.insert.*|sys.path.insert(0, "/usr/local/lib/gpu-power")|' "$repo/src/gpu-power.py" > "$work/gpu-power"
install -Dm755 "$work/gpu-power" /usr/local/bin/gpu-power
install -m644 "$work/runtime.json" /etc/gpu-power-policy.json
modconf=/etc/modprobe.d/gpu-power-policy.conf
dracutconf=/etc/dracut.conf.d/gpu-power-policy.conf
dropin=/etc/systemd/system/nvidia-powerd.service.d/gpu-power-policy.conf
init=/etc/systemd/system/gpu-power-policy-init.service
mkdir -p /etc/modprobe.d /etc/dracut.conf.d "$(dirname "$dropin")"
if [[ -n $rom ]]; then
    install -Dm444 "$work/payload.bin" "/lib/firmware/$firmware"
    install -m644 "$work/plan/modprobe.conf" "$modconf"
    install -m644 "$work/plan/dracut.conf" "$dracutconf"
    install -m644 "$work/plan/powerd.conf" "$dropin"
    install -m644 "$work/plan/gpu-power-policy-init.service" "$init"
    install -Dm644 /usr/share/doc/NVIDIA_GLX-1.0/nvidia-dbus.conf /etc/dbus-1/system.d/nvidia-dbus.conf
    systemctl enable nvidia-powerd.service
else
    rm -f "$modconf" "$dracutconf" "$dropin" "$init"
fi
dracut --force --kver "$kernel"
systemctl daemon-reload
echo "Installed for $kernel. Reboot into that kernel before running gpu-power."
