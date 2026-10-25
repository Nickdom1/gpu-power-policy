#!/usr/bin/env bash
# Run every CPU-only test. Needs python3 (with pytest), gdb, binutils (as, ld) and gcc.
# Nothing here touches a GPU, NVIDIA library, systemd unit or the network.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONDONTWRITEBYTECODE=1
for tool in python3 gdb as ld gcc; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
python3 -c "import pytest" 2>/dev/null || { echo "missing: python3 pytest" >&2; exit 1; }
python3 -m pytest -q -p no:cacheprovider \
  tools/current-limit/tests tools/policy-status/tests tools/boot-vbios/tests tools/tests \
  module/tests/test_startup.py
python3 module/tests/test_setter.py
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
python3 tools/policy-status/tests/test_capture.py --output-dir "$tmp/capture"
python3 tools/boot-vbios/tests/test-collector.py
python3 module/tests/test_image_edit.py
python3 tools/policy-125/tests/test-guard.py
( cd module/nvpcf
  gcc -std=gnu11 -Wall -Wextra -Werror -O1 -o "$tmp/t2" test-runtime.c && "$tmp/t2"
  gcc -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -o "$tmp/libnvpcf-shim.so" nvpcf-shim.c -ldl )
echo "all CPU tests passed"
