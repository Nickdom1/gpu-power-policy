"""The shared pins are literal data, never executable configuration."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
LINES = [line for line in (ROOT / "install/pins.sh").read_text().splitlines() if line and not line.startswith("#")]
PINS = dict(line.split("=", 1) for line in LINES)
# Hardware captures are recorded and adjudicated before updating this pin.
FIXTURES = ROOT / "tests/fixtures" / PINS["fixture_version"]
RAISED_FIXTURES = FIXTURES.parent / "615.71.09"


class PinFormat(unittest.TestCase):
    def test_strict_literal_format_and_editor_lock(self):
        self.assertEqual(len(PINS), len(LINES), "duplicate pin")
        for line in LINES:
            self.assertRegex(line, r"^[a-z_][a-z0-9_]*=([a-z0-9._/-]+|'[a-z0-9._/ -]+')$", line)
        self.assertEqual(json.loads((ROOT / "flake.lock").read_text())["nodes"]["vix-editor"]["locked"]["rev"],
                         PINS["editor_commit"])
        version = re.search(r'^VERSION = "([^"\n]+)"$', (ROOT / "src/rm.py").read_text(), re.M)
        self.assertEqual(version[1], PINS["version"])
        self.assertTrue(FIXTURES.is_dir())
