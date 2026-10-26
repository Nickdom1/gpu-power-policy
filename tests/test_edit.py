import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.environ["EDITOR_TESTS"])
from test_rom_formats import synthetic_rom

BUILDER = Path(__file__).resolve().parents[1] / "src/make-image.py"
# Optional: a saved ROM of the validated board; the payload hash below is the booted image.
PRIVATE_ROM = Path(os.environ.get("GPU_POWER_PRIVATE_ROM", ""))
CHANGES = dict(power_max_w=140, power_default_w=80,
               nvvdd_output_ocp_max_ma=120000, nvvdd_output_ocp_rated_ma=99000)


class EditTests(unittest.TestCase):
    def build(self, blob):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.rom").write_bytes(blob)
            (root / "changes.json").write_text(json.dumps(CHANGES))
            result = subprocess.run([sys.executable, str(BUILDER), "input.rom", "changes.json",
                                     "edited.rom", "payload.bin"], cwd=root, capture_output=True, text=True)
            if result.returncode:
                return result, None, None
            return result, (root / "payload.bin").read_bytes(), json.loads((root / "edited.rom.json").read_text())

    def test_synthetic_ampere(self):
        blob = bytearray(synthetic_rom(family="ampere", policies=True, prefix=0))
        # The upstream edit fixture terminates at EFI; relocation walks on to NPDS.
        efi = 2048
        blob[efi + 32 + 21] = 0
        blob[efi + 511] = (blob[efi + 511] - sum(blob[efi:efi + 512])) & 255
        result, payload, report = self.build(blob)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLessEqual(len(payload), 512 * 1024)
        self.assertEqual(payload[:2], b"\x55\xaa")
        fields = {f["key"]: f["current"] for f in report["after"]["fields"]}
        self.assertEqual({k: fields[k] for k in CHANGES}, CHANGES)

    def test_malformed_refuses(self):
        result, payload, _ = self.build(b"not a ROM")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported VBIOS family", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIsNone(payload)

    @unittest.skipUnless(PRIVATE_ROM.name and os.access(PRIVATE_ROM, os.R_OK), "GPU_POWER_PRIVATE_ROM is not set")
    def test_private_payload_matches_previously_booted_image(self):
        result, payload, _ = self.build(PRIVATE_ROM.read_bytes())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(payload).hexdigest(),
                         "57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73")


if __name__ == "__main__":
    unittest.main()
