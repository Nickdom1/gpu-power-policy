#!/usr/bin/env bash
# P13 ladder, root-run. Envelope boot (mode 4) or default boot (mode 3; then only C-1 + null run).
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
py=$(command -v python3 || echo /run/current-system/sw/bin/python3)
exec "$py" "$here/ladder.py" "$@"
