import ctypes
import os
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import policy
import rm


def buffers(index=7):
    info, control = bytearray(policy.SIZES[policy.INFO]), bytearray(policy.SIZES[policy.GET])
    struct.pack_into("<I", info, 4, 1 << index)
    info[0xCC + index * 0x104 + 4] = 15
    struct.pack_into("<4I", info, 0xCC + index * 0x104 + 8, 1, 99000, 120000, 0)
    control[12] = 255
    struct.pack_into("<I", control, 0x10, 1 << index)
    control[0x14 + index * 0xC4] = 15
    struct.pack_into("<I", control, 0x18 + index * 0xC4, 99000)
    return info, control


class TransportTests(unittest.TestCase):
    def test_layouts(self):
        self.assertEqual(rm.NVOS21.size, 32)
        self.assertEqual(rm.NVOS54.size, 32)
        raw = rm.NVOS54.pack(1, 2, policy.GET, 0, 0x123456789ABC, 0x3634, 9)
        self.assertEqual(struct.unpack_from("<QII", raw, 16), (0x123456789ABC, 0x3634, 9))
        self.assertEqual(rm.ioctl_number(0x2A, 32), 0xC020462A)
        self.assertEqual(rm.ioctl_number(210, 72), 0xC04846D2)
        card = bytearray(72)
        card[0] = 1
        struct.pack_into("<IBBB", card, 4, 3, 4, 5, 1)
        struct.pack_into("<I", card, 16, 42)
        struct.pack_into("<I", card, 56, 6)
        self.assertEqual(rm.cards(card), [{"bdf": "0003:04:05.1", "gpu_id": 42, "minor": 6}])

    def test_session_selects_bdf_and_allocates_public_structures(self):
        seen = []
        def ioctl(fd, request, raw, mutate):
            self.assertEqual((fd, mutate), (12, True))
            command = request & 255
            seen.append(command)
            if command == 210:
                self.assertEqual(raw[:8], bytes(8))
                self.assertEqual(raw[8:17], b"615.71.09")
                struct.pack_into("<I", raw, 4, 1)
            elif command == 200:
                raw[72] = 1
                struct.pack_into("<IBBB", raw, 76, 0, 1, 0, 0)
                struct.pack_into("<I", raw, 88, 42)
                struct.pack_into("<I", raw, 128, 9)
            elif command in (0x2B, 0x2A):
                root, parent, obj, cls, ptr, size, status = rm.NVOS21.unpack(raw)
                if command == 0x2B:
                    if cls == 0x41:
                        self.assertEqual((root, parent, obj, size, ptr), (0, 0, 0, 0, 0))
                        struct.pack_into("<I", raw, 8, 0xCAFE)
                    elif cls == 0x80:
                        self.assertEqual(open_node.call_args_list[-1],
                                         call("/dev/nvidia9", rm.os.O_RDWR | rm.os.O_CLOEXEC))
                        close.assert_not_called()
                        self.assertEqual((root, parent, obj, size), (0xCAFE, 0xCAFE, 1, 56))
                        self.assertEqual(ctypes.string_at(ptr, size), struct.pack("<I", 3) + bytes(52))
                    else:
                        self.assertEqual((parent, cls, size), (1, 0x2080, 4))
                        self.assertEqual(ctypes.string_at(ptr, size), struct.pack("<I", 2))
                else:
                    params = (ctypes.c_ubyte * size).from_address(ptr)
                    if obj == 0x215:
                        self.assertEqual(bytes(params[:8]), struct.pack("<II", 42, 0xFFFFFFFF))
                    elif obj == 0x205:
                        params[8:16] = struct.pack("<II", 3, 2)
                    else:
                        params[0] = 33
            elif command == 0x29:
                self.assertEqual(struct.unpack("<IIII", raw), (0xCAFE, 0, 0xCAFE, 0))
        with patch.object(rm.os, "open", side_effect=[12, 13]) as open_node, patch.object(rm.os, "close") as close:
            with patch.object(rm.fcntl, "ioctl", side_effect=ioctl), rm.RM("0000:01:00.0") as client:
                status, data = client.control(policy.STATUS, bytes(policy.SIZES[policy.STATUS]))
                self.assertEqual((status, data[0], len(data)), (0, 33, policy.SIZES[policy.STATUS]))
            self.assertEqual(close.call_args_list, [call(13), call(12)])
        self.assertEqual(seen[:3], [210, 0x2B, 200])
        self.assertEqual(seen[-1], 0x29)

    def test_status_and_size_refusals(self):
        with self.assertRaisesRegex(RuntimeError, "0x00000056"):
            rm.RM.check(0x56, "test")
        for command, old_size in ((policy.INFO, 0x21B8), (policy.GET, 0x18D4), (policy.STATUS, 0x2E740)):
            with self.assertRaisesRegex(ValueError, "reply size"):
                policy.checked(command, bytes(old_size))
        # Optional: a raw 595-series A61A reply, which must be refused by size.
        capture = Path(os.environ.get("GPU_POWER_595_CAPTURE", ""))
        if capture.name and capture.is_file():
            with self.assertRaisesRegex(ValueError, "reply size"):
                policy.checked(policy.GET, capture.read_bytes())


class PolicyTests(unittest.TestCase):
    def test_discovery_and_bounds(self):
        info, control = buffers(7)
        self.assertEqual(policy.current_policy(info, control, 99000, 110000), (7, 99000, 99000, 110000))
        struct.pack_into("<I", info, 0xCC + 7 * 0x104 + 16, 105000)
        self.assertEqual(policy.current_policy(info, control, 99000, 120000)[3], 105000)
        control[0x14 + 7 * 0xC4] = 3
        with self.assertRaises(ValueError):
            policy.current_policy(info, control, 99000, 120000)

    def test_write_gate_and_one_word_roundtrip(self):
        info, control = buffers()
        class Fake:
            def control(self, command, raw):
                if command == policy.SET:
                    expected = bytearray(control)
                    struct.pack_into("<I", expected, 0x18 + 7 * 0xC4, 107000)
                    assert raw == expected
                    control[:] = raw
                return 0, bytes(control)
        with patch.object(policy, "CURRENT_LAYOUT_VERIFIED", False):
            with self.assertRaisesRegex(ValueError, "SET disabled"):
                policy.set_current(Fake(), info, 99000, 120000, 107000)
        with patch.object(policy, "CURRENT_LAYOUT_VERIFIED", True):
            self.assertEqual(policy.set_current(Fake(), info, 99000, 120000, 107000), 107000)
            with self.assertRaisesRegex(ValueError, "within"):
                policy.set_current(Fake(), info, 99000, 120000, 120001)

    def test_ambiguous_policy_and_readback_refuse(self):
        info, control = buffers()
        extra, _ = buffers(9)
        info[0xCC + 9 * 0x104:0xCC + 10 * 0x104] = extra[0xCC + 9 * 0x104:0xCC + 10 * 0x104]
        struct.pack_into("<I", info, 4, (1 << 7) | (1 << 9))
        with self.assertRaisesRegex(ValueError, "not unique"):
            policy.current_policy(info, control, 99000, 120000)
        info, control = buffers()
        class Unchanged:
            def control(self, command, raw):
                return 0, bytes(control)
        with patch.object(policy, "CURRENT_LAYOUT_VERIFIED", True):
            with self.assertRaisesRegex(RuntimeError, "readback"):
                policy.set_current(Unchanged(), info, 99000, 120000, 107000)
            control[:16] = bytes(16)
            with self.assertRaisesRegex(ValueError, "header"):
                policy.set_current(Unchanged(), info, 99000, 120000, 107000)


class LiveFixtureTests(unittest.TestCase):
    # RTX 3080 Ti Laptop GPU, driver 615.71.09, image 140/80 W and 120000/99000 mA; read-only GETs.
    fixtures = Path(__file__).parent / "fixtures/615.71.09"

    def test_live_tables_and_current_roundtrip(self):
        info = (self.fixtures / "a618.bin").read_bytes()
        full = (self.fixtures / "a61a.bin").read_bytes()
        single = (self.fixtures / "a61a-current.bin").read_bytes()
        status = (self.fixtures / "a619.bin").read_bytes()
        self.assertTrue(policy.CURRENT_LAYOUT_VERIFIED)
        self.assertEqual(policy.u32(info, 4), 0x7F21F)
        self.assertEqual(policy.table(policy.INFO, info)[2]["words"][:3], [1000, 80000, 140000])
        for raw in (full, single):
            self.assertEqual(policy.current_policy(info, raw, 99000, 120000), (13, 99000, 99000, 120000))
        policy.checked(policy.STATUS, status)
        self.assertEqual(policy.u32(status, 4), policy.u32(info, 4))
        self.assertEqual(policy.u32(single, 0x10), 1 << 13)
        self.assertEqual(single[0xA08], 15)
        self.assertEqual(policy.u32(single, 0xA0C), 99000)
        test = self
        class Captured:
            current = single
            writes = 0
            def control(self, command, raw):
                if command == policy.SET:
                    test.assertEqual(raw[:0xA0C], self.current[:0xA0C])
                    test.assertEqual(raw[0xA10:], self.current[0xA10:])
                    self.current = bytes(raw)
                    self.writes += 1
                    return 0, self.current
                test.assertEqual(command, policy.GET)
                return 0, full if policy.u32(raw, 0x10) == 0x7F21F else self.current
        client = Captured()
        for value in (107000, 99000):
            self.assertEqual(policy.set_current(client, info, 99000, 120000, value), value)
        self.assertEqual(client.writes, 2)
        for offset in (0, 12, 0x3114, 0x3298):
            malformed = bytearray(single)
            malformed[offset] ^= 1
            client.current = bytes(malformed)
            with self.assertRaisesRegex(ValueError, "header"):
                policy.set_current(client, info, 99000, 120000, 107000)
        self.assertEqual(client.writes, 2)


if __name__ == "__main__":
    unittest.main()
