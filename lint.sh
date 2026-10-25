#!/usr/bin/env bash
# CPU-only lint and formatting checks. Run with: nix-shell --run ./lint.sh
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for tool in ruff shellcheck deadnix nixfmt; do
  command -v "$tool" >/dev/null || { echo "missing: $tool (use nix-shell)" >&2; exit 1; }
done
ruff check --no-cache .
ruff format --check --no-cache .
shellcheck -x -P SCRIPTDIR test.sh lint.sh tools/*.sh tools/*/*.sh
deadnix --fail shell.nix module/*.nix module/nvpcf/*.nix
nixfmt --check shell.nix module/*.nix module/nvpcf/*.nix
