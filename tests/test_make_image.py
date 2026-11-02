import contextlib
import copy
import gc
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_first_run import ROM

BUILDER = Path(__file__).resolve().parents[1] / "src/make-image.py"
spec = importlib.util.spec_from_file_location("image", BUILDER)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)


class ImageErrors(unittest.TestCase):
    def test_missing_editor_is_actionable_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            changes = Path(directory) / "changes.json"
            changes.write_text('{"device":"0000:01:00.0"}')
            result = subprocess.run(
                [sys.executable, str(BUILDER), "input", str(changes), directory],
                env={**os.environ, "PATH": ""},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nvbios command not found", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_missing_field_is_named_and_changes_file_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            changes = Path(directory) / "changes.json"
            changes.write_text(json.dumps({"unknown": 1}))
            replies = [{"architecture": {"family": "ampere"}}, {"affected_fields": [], "after": {"fields": []}}]
            with warnings.catch_warnings(record=True) as caught, patch.object(image, "nvbios", side_effect=replies):
                warnings.simplefilter("always", ResourceWarning)
                with self.assertRaisesRegex(ValueError, "requested field: unknown"):
                    image.main("input", changes, directory)
                gc.collect()
                self.assertFalse([warning for warning in caught if warning.category is ResourceWarning])

    def test_output_contract_preserves_rom_seeds_and_refuses_oversize(self):
        for size in (128, image.GSP_IMAGE_LIMIT + 1):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                request = root / "request.json"
                request.write_text(json.dumps({"device": "0000:01:00.0", "currentCeilingMilliamps": 107000}))
                after = copy.deepcopy(ROM)
                after["values"]["currentCeilingMilliamps"] = 107000
                def editor(command, *args):
                    if command == "edit":
                        return {"affected_fields": []}
                    payload = Path(args[1])
                    payload.write_bytes(b"x" * size)
                    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
                    Path(str(payload) + ".json").write_text(json.dumps({"output_size": size, "output_sha256": digest}))
                    return {"sha256": digest}
                output = root / "out"
                with patch.object(image.baseline, "read_rom", side_effect=[ROM, after]), patch.object(
                    image, "nvbios", side_effect=editor
                ), contextlib.redirect_stdout(io.StringIO()):
                    if size > image.GSP_IMAGE_LIMIT:
                        with self.assertRaisesRegex(SystemExit, "GSP limit"):
                            image.main("input.rom", request, output)
                        self.assertFalse((output / "runtime.json").exists())
                        self.assertFalse((output / "modprobe.conf").exists())
                    else:
                        image.main("input.rom", request, output)
                        runtime = json.loads((output / "runtime.json").read_text())
                        self.assertEqual(runtime["powerDefaultWatts"], 80)
                        self.assertEqual(runtime["currentRatedMilliamps"], 99000)
                        self.assertEqual(runtime["romBaseline"], ROM)
                        self.assertEqual(runtime["currentCeilingMilliamps"], 107000)
                        self.assertIn('RmLinuxVbiosOverrideSize=128', (output / "modprobe.conf").read_text())
                        self.assertIn('pci=0000:01:00.0', (output / "modprobe.conf").read_text())


if __name__ == "__main__":
    unittest.main()
