import importlib.util
from pathlib import Path
import struct
import json
import subprocess
import sys
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / "collect-boot-vbios.py"
spec = importlib.util.spec_from_file_location("collector", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def snapshot(state=1, image=b"\x55\xaa" + bytes(510), rm=0):
    return (
        b"NVBROM1\0"
        + struct.pack(
            "<14I", 1, 64, state, rm, len(image), 0, 0, 1, 0, 0, 0x10DE, 0x2460, 0, 0
        )
        + image
    )


class DecoderTests(unittest.TestCase):
    def test_captured(self):
        r = m.decode(snapshot())
        self.assertEqual(r["image_bytes"], 512)
        self.assertEqual(r["state"], "captured")
        self.assertTrue(r["starts_55aa"])
        self.assertFalse(r["full_nvidia_image_verified"])

    def test_failure_states(self):
        for state in [0, 2, 3, 4]:
            with self.subTest(state=state):
                r = m.decode(snapshot(state, b"", 0x56 if state == 2 else 0))
                self.assertEqual(r["state"], m.STATES[state])
                self.assertNotIn("image_sha256", r)

    def test_bounds_and_shape(self):
        good = snapshot()
        for bad in [
            b"",
            good[:63],
            good[:-1],
            good + b"\0",
            snapshot(image=b"X" * (m.MAX_IMAGE + 1)),
            snapshot(image=b""),
            snapshot(state=2),
            snapshot(rm=1),
        ]:
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    m.decode(bad)

    def test_header_corruption(self):
        good = snapshot()
        for offset, value in [
            (8, 2),
            (12, 63),
            (16, 5),
            (28, 513),
            (32, 1),
            (36, 2),
            (40, 1),
            (44, 1),
            (48, 0),
            (52, 0),
            (56, 1),
            (60, 1),
        ]:
            with self.subTest(offset=offset):
                bad = bytearray(good)
                struct.pack_into("<I", bad, offset, value)
                with self.assertRaises(ValueError):
                    m.decode(bad)
        with self.assertRaises(ValueError):
            m.decode(b"badmagic" + good[8:])

    def test_maximum_size(self):
        self.assertEqual(
            m.decode(snapshot(image=bytes(m.MAX_IMAGE)))["image_bytes"], m.MAX_IMAGE
        )

    def check_cli(self, blob, expected_exit, image_expected):
        with tempfile.TemporaryDirectory(prefix="boot-vbios-offline-test-") as tmp:
            tmp = Path(tmp)
            source, out = tmp / "fixture.bin", tmp / "out"
            source.write_bytes(blob)
            r = subprocess.run(
                [
                    sys.executable,
                    str(path),
                    "--decode",
                    str(source),
                    "--output-dir",
                    str(out),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, expected_exit, r.stdout + r.stderr)
            self.assertEqual((out / "snapshot.bin").read_bytes(), blob)
            self.assertEqual((out / "flash-copy.bin").exists(), image_expected)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            for f in out.iterdir():
                self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads((out / "capture.json").read_text())["mode"], "offline_decode"
            )

    def test_cli_capture(self):
        self.check_cli(snapshot(), 0, True)

    def test_cli_failed_extraction(self):
        self.check_cli(snapshot(2, b"", 0x56), 2, False)

    def test_cli_malformed_preserves_raw(self):
        self.check_cli(snapshot()[:-1], 1, False)


if __name__ == "__main__":
    unittest.main()
