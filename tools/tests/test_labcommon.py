"""Parsing used by the telemetry collector; CPU-only."""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import labcommon as lc


class Parsing(unittest.TestCase):
    def test_parse_value(self):
        for na in (
            "[N/A]",
            "N/A",
            "[Not Supported]",
            "",
            "  ",
            "[Unknown Error]",
            "NA",
            "[Requested functionality has been deprecated]",
        ):
            self.assertIsNone(lc.parse_value(na), na)
        self.assertEqual(lc.parse_value("Active"), 1)
        self.assertEqual(lc.parse_value("Not Active"), 0)
        self.assertEqual(lc.parse_value("0x00000000000001FF"), 0x1FF)
        self.assertEqual(lc.parse_value(" 80.00 "), 80.0)
        self.assertEqual(lc.parse_value("P8"), "P8")
        self.assertEqual(lc.parse_value("none"), "none")  # "no holders" is a value
        self.assertEqual(
            lc.parse_value("Unknown"), "Unknown"
        )  # a battery status, not a gap

    def test_header_normalization(self):
        self.assertEqual(lc.norm_gpu_header("clocks.current.sm [MHz]"), "clocks.sm")
        self.assertEqual(lc.norm_gpu_header(" power.draw [W]"), "power.draw")
        self.assertEqual(
            lc.norm_gpu_header("clocks_throttle_reasons.sw_power_cap"),
            "clocks_event_reasons.sw_power_cap",
        )
        self.assertEqual(
            lc.norm_gpu_header("clocks_event_reasons_counters.sw_power_cap [us]"),
            "clocks_event_reasons_counters.sw_power_cap",
        )

    def test_event_line_roundtrip(self):
        line = lc.event_line(1792000000.0, "src", "tab\there\nnewline")
        _, epoch, source, message = line.rstrip("\n").split("\t")
        self.assertEqual((source, message), ("src", "tab here newline"))
        self.assertEqual(float(epoch), 1792000000.0)
