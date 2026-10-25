#!/usr/bin/env bash
# Collect 1 Hz GPU telemetry, AC/Turbo state and kernel/powerd logs.
# Usage: sudo ./collect.sh RUN_ID [--pre S] [--post S] [--runs-dir DIR] -- CMD [ARG...]
# CMD runs as the invoking desktop user. Runs are never overwritten.
set -euo pipefail
shopt -s nullglob

LAB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB=$LAB_DIR/lib
export PYTHONDONTWRITEBYTECODE=1 # no root-owned __pycache__ in the lab directory
PY=$(command -v python3 || echo /run/current-system/sw/bin/python3)
SUDO_BIN=$(command -v sudo || echo /run/wrappers/bin/sudo)

die() { printf 'collect.sh: %s\n' "$*" >&2; exit 2; }
usage() { awk 'NR == 1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"; }

RUN_ID="" PRE="" POST="" RUNS_DIR=$LAB_DIR/runs
CHILD=() ARGV=("$@")
while (($#)); do
  case $1 in
    --pre) PRE=${2?}; shift ;;
    --post) POST=${2?}; shift ;;
    --runs-dir) RUNS_DIR=${2?}; shift ;;
    -h | --help) usage; exit 0 ;;
    --) shift; CHILD=("$@"); break ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [[ -z $RUN_ID ]] || die "unexpected argument $1"; RUN_ID=$1 ;;
  esac
  shift
done

[[ -n $RUN_ID ]] || die "usage: sudo $0 RUN_ID [options] [-- command...]"
if [[ -n $RUN_ID && ! $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  die "RUN_ID may use letters, digits, '.', '_' and '-' (max 64)"
fi
for v in "${PRE:-0}" "${POST:-0}"; do [[ $v =~ ^[0-9]+$ ]] || die "--pre/--post take whole seconds"; done
if ((${#CHILD[@]})); then PRE=${PRE:-10} POST=${POST:-10}; else PRE=${PRE:-0} POST=${POST:-0}; fi

# --------------------------------------------------------------- who we run for
((EUID == 0)) || die "needs root: run with sudo from your desktop terminal"
TARGET_USER=${SUDO_USER:-}
[[ -n $TARGET_USER && $TARGET_USER != root ]] || die "run with sudo from your desktop terminal (SUDO_USER is unset)"
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_GID=$(id -g "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)

# --------------------------------------------------------------- user session env
# sudo strips WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS and friends. The exact
# environment of the shell that ran sudo is still in the sudo process's
# /proc/PID/environ (root can read it), so CMD gets the same session as a
# normal launch from that terminal.
find_sudo_environ() {
  local p=$PPID comm i
  for ((i = 0; i < 12 && p > 1; i++)); do
    comm=$(cat "/proc/$p/comm" 2>/dev/null) || return 1
    if [[ $comm == sudo ]]; then
      printf '%s\n' "/proc/$p/environ"
      return 0
    fi
    p=$(awk '/^PPid:/ {print $2}' "/proc/$p/status" 2>/dev/null) || return 1
  done
  return 1
}

USER_ENV=() ENV_SOURCE=none
build_user_env() {
  local src entry key line
  local -a raw=()
  local -A seen=()
  if ((EUID == 0)) && src=$(find_sudo_environ); then
    mapfile -d '' raw <"$src"
    ENV_SOURCE="environ of the sudo process ($src)"
  elif ((EUID != 0)); then
    mapfile -d '' raw < <(env -0)
    ENV_SOURCE="current environment (not root)"
  fi
  USER_ENV=()
  for entry in "${raw[@]}"; do
    [[ $entry == *=* ]] || continue
    key=${entry%%=*}
    case $key in SUDO_* | LAB_* | LD_PRELOAD) continue ;; esac
    USER_ENV+=("$entry")
    seen[$key]=1
  done
  if [[ -z ${seen[WAYLAND_DISPLAY]:-} ]] && ((EUID == 0)) && [[ $TARGET_USER != root ]]; then
    # Fallback: the user manager's environment (Plasma exports its session there).
    while IFS= read -r line; do
      key=${line%%=*}
      case $key in WAYLAND_DISPLAY | DISPLAY | XAUTHORITY | XDG_SESSION_TYPE | XDG_CURRENT_DESKTOP | DBUS_SESSION_BUS_ADDRESS) ;; *) continue ;; esac
      [[ $line == *"=\$'"* || -n ${seen[$key]:-} ]] && continue
      USER_ENV+=("$line")
      seen[$key]=1
      ENV_SOURCE+=" + user manager $key"
    done < <(timeout 10 systemctl --user -M "$TARGET_USER@" show-environment 2>/dev/null || true)
  fi
  local -a defaults=("HOME=$TARGET_HOME" "USER=$TARGET_USER" "LOGNAME=$TARGET_USER"
    "XDG_RUNTIME_DIR=/run/user/$TARGET_UID" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$TARGET_UID/bus"
    "PATH=/run/wrappers/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin")
  for entry in "${defaults[@]}"; do
    [[ -n ${seen[${entry%%=*}]:-} ]] || USER_ENV+=("$entry")
  done
  local have=""
  for key in WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR; do
    [[ -n ${seen[$key]:-} ]] && have+=" $key"
  done
  ENV_SOURCE+="; session vars present:${have:- none}"
  if [[ -n ${RUN_DIR:-} ]]; then
    USER_ENV+=("LAB_RUN_ID=$RUN_ID" "LAB_RUN_DIR=$RUN_DIR" "LAB_EVENTS=$RUN_DIR/events.log" "LAB_DIR=$LAB_DIR")
  fi
  # Settings the runner passes to the workload launcher (witness duration) travel
  # in this process's environment, not the sudo caller's; carry them over.
  while IFS= read -r -d '' entry; do
    key=${entry%%=*}
    [[ $key == PT516_* && -z ${seen[$key]:-} ]] || continue
    USER_ENV+=("$entry")
    seen[$key]=1
  done < <(env -0)
}

# Runs "$@" as the target user with USER_ENV. sudo -u keeps the command (not
# the environment values) in its log because the environment is passed with
# --preserve-env rather than on the command line.
run_as_user() {
  if ((EUID == 0)) && [[ $TARGET_USER != root ]]; then
    env -i "${USER_ENV[@]}" "$SUDO_BIN" -u "$TARGET_USER" --preserve-env -- "$@"
  else
    env -i "${USER_ENV[@]}" "$@"
  fi
}

# --------------------------------------------------------------- run
RUN_DIR=$RUNS_DIR/$RUN_ID
[[ ! -e $RUN_DIR ]] || die "$RUN_DIR exists; runs are never overwritten, pick a new RUN_ID"
umask 022
if [[ ! -d $RUNS_DIR ]]; then
  mkdir -p "$RUNS_DIR"
  ((EUID != 0)) || chown "$TARGET_UID:$TARGET_GID" "$RUNS_DIR"
fi
mkdir "$RUN_DIR" "$RUN_DIR/snapshots" "$RUN_DIR/workload"
: >"$RUN_DIR/events.log"
((EUID != 0)) || chown "$TARGET_UID:$TARGET_GID" "$RUN_DIR" "$RUN_DIR/workload" "$RUN_DIR/events.log"

# shellcheck source=lib/events.sh
. "$LIB/events.sh"
ev() { lab_event "$RUN_DIR/events.log" "$@"; }

STOP=0 STOP_REASON="" FINISHED=0 STARTED=0 SAMPLER_PID="" CHILD_RC=""
# shellcheck disable=SC2329 # called from a trap
on_signal() {
  STOP=1
  STOP_REASON=${STOP_REASON:-$1}
}
trap 'on_signal SIGINT' INT
trap 'on_signal SIGTERM' TERM
trap 'on_signal SIGHUP' HUP

isleep() { # interruptible sleep
  local s=${1:-0} p
  ((s > 0 && STOP == 0)) || return 0
  sleep "$s" &
  p=$!
  wait "$p" || true
  kill "$p" 2>/dev/null || true
  wait "$p" 2>/dev/null || true
}

snapshot() {
  local ph=$1 s=$RUN_DIR/snapshots f
  timeout 30 nvidia-smi -q -d POWER,PERFORMANCE,TEMPERATURE,CLOCK >"$s/nvidia-smi-q-$ph.txt" 2>&1 || true
  if [[ $ph == start ]]; then
    timeout 15 journalctl -b -u nvidia-powerd -n 200 --no-pager -o short-iso-precise \
      >"$s/nvidia-powerd-journal-start.txt" 2>&1 || true
  fi
  for f in /proc/driver/nvidia/gpus/*/power; do cat "$f" >"$s/nvidia-proc-power-$ph.txt" 2>&1 || true; done
  if command -v turbo-broker >/dev/null; then
    timeout 15 turbo-broker status --json >"$s/turbo-broker-status-$ph.json" 2>&1 || true
  fi
  timeout 10 systemctl status nvidia-powerd --no-pager >"$s/nvidia-powerd-status-$ph.txt" 2>&1 || true
  if command -v kscreen-doctor >/dev/null; then
    run_as_user timeout 15 kscreen-doctor -j >"$s/kscreen-$ph.json" 2>&1 || true
  fi
}

# shellcheck disable=SC2329 # called from a trap
stop_sampler() {
  local i
  [[ -n $SAMPLER_PID ]] || return 0
  if kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
      kill -0 "$SAMPLER_PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL "$SAMPLER_PID" 2>/dev/null || true
  fi
  wait "$SAMPLER_PID" 2>/dev/null || true
  SAMPLER_PID=""
}

# shellcheck disable=SC2329 # called from a trap
finish() {
  local rc=$?
  set +e
  trap '' INT TERM HUP
  ((FINISHED)) && return
  FINISHED=1
  ((STARTED)) || return 0
  ev collect "collect stop reason=${STOP_REASON:-normal} exit=$rc"
  # Write before cleanup: no child status means no completed workload.
  printf '{"run_id":"%s","start_epoch":%s,"end_epoch":%s,"stop_reason":"%s","child":{"exit_status":%s}}\n' \
    "$RUN_ID" "$START_EPOCH" "${EPOCHREALTIME/,/.}" "${STOP_REASON:-normal}" "${CHILD_RC:-null}" >"$RUN_DIR/metadata.json"
  stop_sampler
  snapshot end
  rm -f "$RUN_DIR/.sampler-ready"
  ((EUID != 0)) || chown -R "$TARGET_UID:$TARGET_GID" "$RUN_DIR"
  printf '\ncollect.sh: saved %s\n' "$RUN_DIR"
}
trap finish EXIT

START_EPOCH=${EPOCHREALTIME/,/.}
STARTED=1
ev collect "collect start run_id=$RUN_ID argv=${ARGV[*]}"
build_user_env
ev collect "session env: $ENV_SOURCE"
snapshot start

"$PY" "$LIB/sampler.py" run --run-dir "$RUN_DIR" 2>>"$RUN_DIR/snapshots/sampler.stderr" &
SAMPLER_PID=$!
for ((i = 0; i < 75; i++)); do
  [[ -e $RUN_DIR/.sampler-ready ]] && break
  kill -0 "$SAMPLER_PID" 2>/dev/null || break
  sleep 0.2
done
[[ -e $RUN_DIR/.sampler-ready ]] || die "sampler not ready; workload not started"
printf 'collect.sh: logging %s (Ctrl-C stops%s)\n' "$RUN_DIR" "${CHILD[*]:+ the command}"

if ((PRE)); then
  ev collect "pre-roll ${PRE}s"
  isleep "$PRE"
fi
if ((${#CHILD[@]})); then
  if ((STOP == 0)); then
    ev collect "workload-start argv=$(printf '%q ' "${CHILD[@]}")"
    set +e
    run_as_user "${CHILD[@]}" 2>&1 | tee -a "$RUN_DIR/workload.log"
    CHILD_RC=${PIPESTATUS[0]}
    set -e
    ev collect "workload-end rc=$CHILD_RC"
    if ((POST && STOP == 0)); then
      ev collect "post-roll ${POST}s"
      isleep "$POST"
    fi
  fi
else
  ev collect "logging until Ctrl-C"
  wait "$SAMPLER_PID" || true
  ((STOP)) || STOP_REASON="sampler exited by itself"
fi
exit 0
