import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "install/lib.sh"


class InstallerTests(unittest.TestCase):
    def shell(self, script, root):
        return subprocess.run(["bash", "-c", 'source "$1"; ' + script, "test", str(LIB)],
                              cwd=root, capture_output=True, text=True)

    def test_secure_boot_empty_missing_enabled_and_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.shell("require_secure_boot_off efi", root).returncode, 0)
            (root / "efi/efivars").mkdir(parents=True)
            self.assertIn("unavailable", self.shell("require_secure_boot_off efi", root).stderr)
            variable = root / "efi/efivars/SecureBoot-test"
            for value in (b"", bytes(4), bytes(4) + b"\x02", bytes(4) + b"\x01"):
                variable.write_bytes(value)
                result = self.shell("require_secure_boot_off efi", root)
                self.assertNotEqual(result.returncode, 0, value)
                self.assertIn("Secure Boot", result.stderr)
            variable.write_bytes(bytes(5))
            self.assertEqual(self.shell("require_secure_boot_off efi", root).returncode, 0)
            result = self.shell("od() { return 1; }; require_secure_boot_off efi", root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot read", result.stderr)

    def test_bad_download_and_bad_cache_are_removed_and_retry_succeeds(self):
        digest = hashlib.sha256(b"good").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = f'download_driver driver.run https://example.invalid/driver {digest}'
            bad = self.shell('curl() { printf bad > driver.run.tmp; }; ' + command, root)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("Rerun the installer", bad.stderr)
            self.assertFalse((root / "driver.run").exists())
            self.assertFalse((root / "driver.run.tmp").exists())
            (root / "driver.run").write_bytes(b"bad cached download")
            self.assertNotEqual(self.shell(command, root).returncode, 0)
            self.assertFalse((root / "driver.run").exists())
            good = self.shell('curl() { printf good > driver.run.tmp; }; ' + command, root)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual((root / "driver.run").read_bytes(), b"good")

    def test_inline_python_ignores_caller_imports(self):
        source = (REPO / "install/linux.sh").read_text()
        start = source.index('    python3 -I - "$work"')
        block = source[start:source.index("\nPY", start) + 3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "json.py").write_text('raise RuntimeError("caller import executed")\n')
            (root / "runtime.json").write_text("{}")
            (root / "payload.bin.json").write_text('{"output_size":1,"output_sha256":"test"}')
            result = subprocess.run(["bash", "-c", block], cwd=root, env={**os.environ, "work": directory},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"output_size": 1', (root / "runtime.json").read_text())

    def test_venv_module_ignores_caller_imports(self):
        source = (REPO / "install/linux.sh").read_text()
        command = next(line.strip().split(' "$cache/editor"')[0]
                       for line in source.splitlines() if ' -m venv ' in line)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "venv.py").write_text('raise RuntimeError("caller import executed")\n')
            result = subprocess.run(command.split() + ["--help"], cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
