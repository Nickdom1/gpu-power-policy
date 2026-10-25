#!/usr/bin/env bash
# One root-run diagnostic; never installs, changes limits or restarts a service.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
py=$(command -v python3 || echo /run/current-system/sw/bin/python3)
exec "$py" "$here/run.py" "$@"
