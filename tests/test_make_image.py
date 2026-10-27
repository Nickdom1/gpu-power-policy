import gc
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

BUILDER = Path(__file__).resolve().parents[1] / "src/make-image.py"
spec = importlib.util.spec_from_file_location("image", BUILDER)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)


class ImageErrors(unittest.TestCase):
    def test_missing_editor_is_actionable_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            changes = Path(directory) / "changes.json"
            changes.write_text("{}")
            result = subprocess.run([sys.executable, str(BUILDER), "input", str(changes), "edited", "payload"],
                                    env={**os.environ, "PATH": ""}, capture_output=True, text=True)
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
                with self.assertRaisesRegex(SystemExit, "requested field 'unknown'"):
                    image.main("input", changes, "edited", "payload")
                gc.collect()
                self.assertFalse([warning for warning in caught if warning.category is ResourceWarning])


if __name__ == "__main__":
    unittest.main()
