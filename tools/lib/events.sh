# shellcheck shell=bash
# lab_event FILE SOURCE MESSAGE...: append one events.log line in the format of
# labcommon.event_line (local ISO time, epoch, source, message; tab separated).
lab_event() {
  local file=$1 src=$2 e s ms iso tz msg
  shift 2
  e=${EPOCHREALTIME/,/.}
  s=${e%.*} ms=${e#*.}
  ms=${ms:0:3}
  printf -v iso '%(%Y-%m-%dT%H:%M:%S)T' "$s"
  printf -v tz '%(%z)T' "$s"
  msg="$*"
  msg=${msg//$'\t'/ }
  msg=${msg//$'\n'/ }
  printf '%s.%s%s:%s\t%s.%s\t%s\t%s\n' "$iso" "$ms" "${tz:0:3}" "${tz:3}" "$s" "$ms" "$src" "$msg" >>"$file"
}
