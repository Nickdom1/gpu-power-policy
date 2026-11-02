import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_first_run import cli, baseline, policy, firmware

EDITOR_TESTS = os.environ.get("EDITOR_TESTS")
if EDITOR_TESTS:
    sys.path.insert(0, EDITOR_TESTS)
    from test_rom_formats import synthetic_rom
    from test_turing import synthetic_turing

BUILDER = Path(__file__).resolve().parents[1] / "src/make-image.py"
# Optional: a saved ROM of the validated board; the payload hash below is the booted image.
PRIVATE_ROM = Path(os.environ.get("GPU_POWER_PRIVATE_ROM", ""))
CHANGES = dict(
    powerCeilingWatts=140,
    currentCeilingMilliamps=120000,
    beyondTested=True,
)


@unittest.skipUnless(EDITOR_TESTS, "EDITOR_TESTS is not set; use nix flake check for editor integration tests")
class EditTests(unittest.TestCase):
    def build(self, blob, changes=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.rom").write_bytes(blob)
            (root / "changes.json").write_text(json.dumps({"device": "0000:01:00.0", **(changes or {})}))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "input.rom", "changes.json", "."],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                return result, None, None
            return result, (root / "payload.bin").read_bytes(), json.loads((root / "edited.rom.json").read_text())

    def test_source_launcher_ignores_caller_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "json.py").write_text('raise RuntimeError("caller import executed")\n')
            launcher = Path(EDITOR_TESTS).parent / "nvbios"
            (root / "nvbios").write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{launcher}" "$@"\n')
            (root / "nvbios").chmod(0o755)
            (root / "bad.rom").write_bytes(b"not a ROM")
            (root / "changes.json").write_text('{"device":"0000:01:00.0"}')
            result = subprocess.run(
                [sys.executable, str(BUILDER), "bad.rom", "changes.json", "."],
                cwd=root, env={**os.environ, "PATH": str(root) + ":" + os.environ["PATH"]},
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported VBIOS family", result.stderr)
            self.assertNotIn("caller import executed", result.stderr)

    def test_synthetic_ampere(self):
        blob = bytearray(synthetic_rom(family="ampere", policies=True, prefix=0))
        # The upstream edit fixture terminates at EFI; relocation walks on to NPDS.
        efi = 2048
        blob[efi + 32 + 21] = 0
        blob[efi + 511] = (blob[efi + 511] - sum(blob[efi : efi + 512])) & 255
        result, payload, report = self.build(blob)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLessEqual(len(payload), 512 * 1024)
        self.assertEqual(payload[:2], b"\x55\xaa")
        fields = {f["key"]: f["current"] for f in report["after"]["fields"]}
        self.assertEqual(fields["power_default_w"], 70)
        self.assertEqual(fields["nvvdd_output_ocp_rated_ma"], 226800)

    def test_configure_real_editor_three_families_and_mismatch(self):
        # The recorded 615 A618 layout is used with explicit simulated values/classes.
        # This proves parsing/comparison, not live Ada/Turing firmware support.
        for family in ("ampere", "ada", "turing"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rom_path = root / "stock.rom"
                blob = (
                    synthetic_turing(inputs=True) if family == "turing" else synthetic_rom(family=family, policies=True)
                )
                rom_path.write_bytes(blob)
                rom = baseline.read_rom(rom_path)
                self.assertEqual(rom["family"], family)
                pci = root / "0000:01:00.0"
                pci.mkdir()
                vendor, device = rom["pci_device"].split(":")
                (pci / "vendor").write_text("0x" + vendor)
                (pci / "device").write_text("0x" + device)
                config = {"device": pci.name, "systemctl": "systemctl", "dynamicBoostEnabled": False}
                args = cli.parser().parse_args(["configure", "--json"])
                with (
                    patch.object(cli, "PCI_DEVICES", root),
                    patch.object(cli, "read_boot_rom", return_value=(blob, {})),
                    patch.object(cli, "RM"),
                    patch.object(policy, "query", return_value=firmware(rom["values"])) as query,
                    patch.object(cli, "dynamic_boost", return_value=False),
                    patch.object(cli, "service_state", return_value="inactive"),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    generated = json.loads(cli.configure(args, config))
                    self.assertNotIn("powerDefaultWatts", generated)
                    self.assertNotIn("currentRatedMilliamps", generated)
                    self.assertNotIn("beyondTested", generated)
                    self.assertIsNone(generated["vbios"])
                    self.assertIsNone(generated["powerdTargetWatts"])
                    query.return_value = firmware({**rom["values"], "currentRatedMilliamps": 1})
                    with self.assertRaisesRegex(ValueError, "baseline mismatch"):
                        cli.configure(args, config)

    def test_builder_refuses_changed_seed_and_unacknowledged_ceiling(self):
        blob = synthetic_rom(family="ampere", policies=True, prefix=0)
        for changes, message in (
            ({"powerDefaultWatts": 80}, "unknown requested field: powerDefaultWatts"),
            ({"currentRatedMilliamps": 99000}, "unknown requested field: currentRatedMilliamps"),
            ({"powerCeilingWatts": 140}, "+100.0%"),
        ):
            result, payload, _ = self.build(blob, changes)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
            self.assertIsNone(payload)

    def test_malformed_refuses(self):
        result, payload, _ = self.build(b"not a ROM")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported VBIOS family", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIsNone(payload)

    @unittest.skipUnless(PRIVATE_ROM.name and os.access(PRIVATE_ROM, os.R_OK), "GPU_POWER_PRIVATE_ROM is not set")
    def test_private_payload_matches_previously_booted_image(self):
        result, payload, _ = self.build(PRIVATE_ROM.read_bytes(), CHANGES)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), "57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73"
        )


if __name__ == "__main__":
    unittest.main()
