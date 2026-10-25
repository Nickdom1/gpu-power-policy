"""Pure CPU XRes reply/cleanup fixtures; never opens an X display."""

import ctypes as C
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xclient_pid as x


class Fake:
    def __init__(self, pids, rc=0):
        self.pids = pids
        self.rc = rc
        self.destroyed = False
        self.values = (x.Value * len(pids))()
        for i, pid in enumerate(pids):
            self.values[i].length = pid

    def XResQueryClientIds(self, display, n, spec, count, values):
        self.spec = C.cast(spec, C.POINTER(x.Spec))[0]
        C.cast(count, C.POINTER(C.c_long))[0] = len(self.pids)
        C.cast(values, C.POINTER(C.POINTER(x.Value)))[0] = C.cast(
            self.values, C.POINTER(x.Value)
        )
        return self.rc

    def XResGetClientPid(self, value):
        return C.cast(value, C.POINTER(x.Value))[0].length

    def XResClientIdsDestroy(self, n, values):
        self.destroyed = True


class Tests(unittest.TestCase):
    def test_success_zero_and_local_pid(self):
        lib = Fake([3393])
        self.assertEqual(x.query(lib, 1, 0xE00002), 3393)
        self.assertEqual(lib.spec.client, 0xE00002)
        self.assertEqual(lib.spec.mask, 2)
        self.assertTrue(lib.destroyed)

    def test_no_local_pid_stays_unknown(self):
        for pids in ([], [-1], [0]):
            lib = Fake(pids)
            self.assertIsNone(x.query(lib, 1, 1))
            self.assertTrue(lib.destroyed)

    def test_ambiguous_reply_refused_and_freed(self):
        lib = Fake([33, 44])
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            x.query(lib, 1, 1)
        self.assertTrue(lib.destroyed)

    def test_failed_request_not_treated_as_pid(self):
        lib = Fake([3393], rc=1)
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            x.query(lib, 1, 1)
        self.assertFalse(lib.destroyed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
