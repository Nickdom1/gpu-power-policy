#!/usr/bin/env bash
# Launch the fixed fullscreen FurMark workload as the invoking desktop user.
# Usage: bench.sh furmark [--seconds N] [--api vk|gl] [--res WxH]
set -euo pipefail
LAB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/events.sh
. "$LAB_DIR/lib/events.sh"
die() { printf 'bench.sh: %s\n' "$*" >&2; exit 2; }
mark() {
  if [[ -n ${LAB_EVENTS:-} && -w $LAB_EVENTS ]]; then lab_event "$LAB_EVENTS" bench "$*"; fi
  printf '[bench] %s\n' "$*"
}
[[ ${1:-} == furmark ]] || die "usage: bench.sh furmark [--seconds N] [--api vk|gl] [--res WxH]"
shift
W=2560 H=1440 API=vk SECS=600
while (($#)); do
  case $1 in
    --res) [[ ${2:-} =~ ^([0-9]+)x([0-9]+)$ ]] || die "--res WxH"; W=${BASH_REMATCH[1]} H=${BASH_REMATCH[2]}; shift ;;
    --api) API=${2:?}; shift ;;
    --seconds) SECS=${2:?}; shift ;;
    *) die "unknown option $1" ;;
  esac
  shift
done
[[ $SECS =~ ^[1-9][0-9]*$ ]] || die "--seconds requires a positive integer"
case $API in vk | gl) ;; *) die "--api vk|gl" ;; esac
FURMARK=$LAB_DIR/.tools/furmark/bin/furmark
[[ -x $FURMARK ]] || die "run $LAB_DIR/prepare-tools.sh before the sitting"
OUT=${LAB_RUN_DIR:?run through collect.sh}/workload
mkdir -p "$OUT"
# Refuse stale foreign/symlink logs; truncate our own logs before launching.
for log in /tmp/furmark-furmark.log /tmp/furmark-geexlab.log; do
  if [[ -e $log || -L $log ]]; then
    [[ -f $log && -O $log && ! -L $log ]] || die "unsafe existing log: $log"
    : >"$log"
  fi
done
# Clear Intel-only session defaults before selecting the NVIDIA renderer.
OFFLOAD_ENV=(-u VK_DRIVER_FILES -u __EGL_VENDOR_LIBRARY_FILENAMES
  __NV_PRIME_RENDER_OFFLOAD=1 __NV_PRIME_RENDER_OFFLOAD_PROVIDER=NVIDIA-G0
  __GLX_VENDOR_LIBRARY_NAME=nvidia __VK_LAYER_NV_optimus=NVIDIA_only)
mark "bench-start name=furmark res=${W}x${H} fullscreen=1 api=$API"
rc=0
env "${OFFLOAD_ENV[@]}" "$FURMARK" --demo "furmark-$API" --width "$W" --height "$H" \
  --vsync 0 --benchmark --duration-ms $((SECS * 1000)) --no-score-box --fullscreen || rc=$?
mark "bench-end name=furmark rc=$rc"
for log in /tmp/furmark-furmark.log /tmp/furmark-geexlab.log; do
  if [[ -f $log && -O $log && ! -L $log ]]; then cp -- "$log" "$OUT/"; fi
done
exit "$rc"
