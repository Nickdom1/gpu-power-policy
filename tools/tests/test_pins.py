"""Keep independently enforced compatibility pins aligned without loading NVML."""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = json.loads((ROOT / "module/provenance.json").read_text())


@pytest.mark.parametrize(
    "path",
    [
        "module/check-maximum.py",
        "module/request/request-125.gdb",
        "tools/policy-status/codec.py",
    ],
)
def test_nvml_pin(path):
    source = (ROOT / path).read_text()
    match = re.search(r"^EXPECTED = (.+)$", source, re.MULTILINE)
    assert match is not None, path
    assert ast.literal_eval(match[1]) == PIN["nvml_sha256"], path


def test_driver_and_kernel_pins():
    nix = (ROOT / "module/default.nix").read_text()
    guard = (ROOT / "tools/policy-125/guard-boot.sh").read_text()
    for expression, key in [
        (r'kernel\.modDirVersion == "([^"]+)"', "kernel"),
        (r'base\.version == "([^"]+)"', "driver"),
    ]:
        assert re.search(expression, nix)[1] == PIN[key]
    assert re.search(r"\$\(uname -r\) == ([^ ]+)", guard)[1] == PIN["kernel"]
    assert (
        re.search(r"/sys/module/nvidia/version\) == ([^ ]+)", guard)[1] == PIN["driver"]
    )
    for mode, key in [(3, "module_srcversion"), (4, "envelope_module_srcversion")]:
        branch = guard.split(f"{mode})", 1)[1].split(";;", 1)[0]
        assert re.search(r"\$module_srcversion == ([^ ]+)", branch)[1] == PIN[key]


def test_shared_hook_offsets():
    paths = [
        "module/request/request-125.gdb",
        "tools/current-limit/client.gdb",
        "tools/policy-status/capture.gdb",
    ]
    sources = [(ROOT / path).read_text() for path in paths]
    # The setter hooks share an exported symbol; capture uses the constraints getter.
    bases = [re.search(r"address - (0x[0-9a-f]+)", source)[1] for source in sources[:2]]
    assert len(set(bases)) == 1, dict(zip(paths[:2], bases))
    wrappers = [
        re.search(r"class Wrapper.*?base \+ (0x[0-9a-f]+)", source, re.DOTALL)[1]
        for source in sources[1:]
    ]
    assert len(set(wrappers)) == 1
