#!/usr/bin/env bash
# Prepare FurMark before a sitting. This fetches an unfree download via pinned nixpkgs.
set -euo pipefail
LAB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PIN=github:nixos/nixpkgs/ecc58f32d1069a3e3f8a991e2233d365689f748d
if (($# > 1)) || [[ ${1:-furmark} != furmark ]]; then
  echo "usage: $0 [furmark]" >&2
  exit 2
fi
mkdir -p "$LAB_DIR/.tools"
export GIT_TERMINAL_PROMPT=0
NIXPKGS_ALLOW_UNFREE=1 nix build --impure "$PIN#furmark" --out-link "$LAB_DIR/.tools/furmark"
