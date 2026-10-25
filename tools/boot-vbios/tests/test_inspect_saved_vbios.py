import importlib.util
from pathlib import Path
import struct
import unittest

path = Path(__file__).resolve().parents[1] / "inspect-saved-vbios.py"
if not path.exists():
    path = Path(__file__).with_name("inspect-saved-vbios.py")
spec = importlib.util.spec_from_file_location("image_inspector", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    data = bytearray(4096)

    def w16(pos, value):
        struct.pack_into("<H", data, pos, value)

    # Legacy + UEFI + NVIDIA extension. PCIR says UEFI is last; NPDE overrides.
    for off, size, kind, last in [
        (0, 1024, 0, False),
        (1024, 512, 3, False),
        (1536, 2560, 0xE0, True),
    ]:
        w16(off, 0xAA55 if kind != 0xE0 else 0x4E56)
        w16(off + 24, 32)
        pc = off + 32
        data[pc : pc + 4] = b"PCIR" if kind != 0xE0 else b"NPDS"
        w16(pc + 4, 0x10DE)
        w16(pc + 6, 0x2460)
        w16(pc + 10, 24)
        w16(pc + 16, size // 512)
        data[pc + 20] = kind
        data[pc + 21] = 128 if (last or kind == 3) else 0
        ex = off + 64
        data[ex : ex + 4] = b"NPDE"
        w16(ex + 4, 0x101)
        w16(ex + 6, 12)
        w16(ex + 8, size // 512)
        data[ex + 10] = 128 if last else 0
    bit = 128
    data[bit : bit + 6] = b"\xff\xb8BIT\0"
    w16(bit + 6, 0x100)
    data[bit + 8 : bit + 11] = bytes([12, 6, 1])
    data[bit + 11] = (-sum(data[bit : bit + 11])) & 255
    data[140:142] = b"P\x02"
    w16(142, 232)
    w16(144, 256)
    struct.pack_into("<I", data, 256 + 44, 1152)  # physical1664 after512 relocation
    struct.pack_into(
        "<I", data, 256 + 244, 999999
    )  # Outside P! Must not become a seed pointer.
    table = 1664
    data[table : table + 4] = bytes([0x30, 44, 71, 7])
    for i in range(7):
        e = table + 44 + 71 * i
        data[e] = 1
        struct.pack_into("<3I", data, e + 2, 1000, 80000, 115000)
    e = table + 44 + 71 * 6
    data[e] = 0
    struct.pack_into("<I", data, e + 10, 175000)
    for off, size in [(0, 1024), (1024, 512), (1536, 2560)]:
        data[off + size - 1] = (-sum(data[off : off + size])) & 255
    return data


class SavedImageTests(unittest.TestCase):
    def test_npde_includes_private_extension(self):
        result = m.analyze(fixture(), 512)
        self.assertEqual(len(result["chain"]), 3)
        self.assertTrue(result["chain"][1]["pcir_last"])
        self.assertFalse(result["chain"][1]["effective_last"])
        self.assertTrue(result["all_subimage_checksums_zero"])
        self.assertEqual(result["policy"]["offset"], 1664)

    def test_short_p_does_not_read_neighbor_data(self):
        result = m.analyze(fixture(), 512)
        self.assertIsNone(result["stored_action_pointer"]["raw_value"])
        self.assertFalse(result["stored_action_pointer"]["physically_present"])
        self.assertEqual(
            result["stored_action_pointer"]["parser_output_field_value"], 0
        )

    def test_dormant_value_does_not_create_policy(self):
        result = m.analyze(fixture(), 512)
        entry = result["policy"]["entries"][6]
        self.assertEqual(entry["min_rated_max_raw"][2], 175000)
        self.assertFalse(entry["passes_initial_type_filter"])

    def test_type_high_byte_is_used(self):
        data = fixture()
        data[1664 + 44 + 71 * 6 + 66] = 1
        entry = m.analyze(data, 512)["policy"]["entries"][6]
        self.assertEqual(entry["raw_type"], 16)
        self.assertTrue(entry["passes_initial_type_filter"])

    def test_bad_bit_checksum_rejected(self):
        data = fixture()
        data[139] ^= 1
        with self.assertRaises(ValueError):
            m.analyze(data, 512)

    def test_changed_checksum_is_reported_not_authenticated(self):
        data = fixture()
        data[900] ^= 1
        result = m.analyze(data, 512)
        self.assertFalse(result["all_subimage_checksums_zero"])
        self.assertFalse(result["cryptographic_signature_verified"])

    def test_chain_bounds_and_termination(self):
        data = fixture()
        for bad in [data[:-1], data + b"X"]:
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                m.parse_chain(bad)
        for value in (0, 65535):
            bad = bytearray(data)
            struct.pack_into("<H", bad, 72, value)
            with self.subTest(length=value), self.assertRaises(ValueError):
                m.parse_chain(bad)
        data[1088:1092] = b"XXXX"  # Without NPDE, premature PCIR termination must fail.
        with self.assertRaises(ValueError):
            m.parse_chain(data)

    def test_relocation_and_policy_bounds(self):
        with self.assertRaises(ValueError):
            m.analyze(fixture(), 0)
        data = fixture()
        struct.pack_into("<I", data, 300, 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            m.analyze(data, 512)

    def test_unknown_layout_is_rejected(self):
        for offset, value in [(1664, 0x40), (1665, 43), (1666, 70), (1667, 255)]:
            data = fixture()
            data[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                m.analyze(data, 512)


if __name__ == "__main__":
    unittest.main()
