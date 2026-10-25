"""CPU-only regression tests for the capture fixes and the panel pair. No GPU/display reads."""

import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parent / "tests")]
import codec
import run
import series
import witness
from test_capture import info_blob, status_blob


class Decode(unittest.TestCase):
    def test_signed_state_arbiter_and_clock_disagreement(self):
        b = bytearray(status_blob())
        off = 0x80 + 2 * 0x168C
        struct.pack_into("<i", b, off + 16, -12345)
        struct.pack_into("<III", b, 8, 4, 1000000, 900000)
        d = codec.status(b, info_blob())
        p = next(p for p in d["policies"] if p["index"] == 2)
        self.assertEqual(p["base16_signed_raw"], -12345)
        self.assertFalse(d["clock_comparison"]["header_equals_min"])
        self.assertEqual(d["clock_comparison"]["exact_matches"], [[], []])

    def test_bad_arbiter_count_refused(self):
        b = bytearray(status_blob())
        b[0x80 + 177] = 9
        with self.assertRaisesRegex(ValueError, "count"):
            codec.status(b, info_blob())

    def test_ties_retained_not_unique_winner(self):
        d = codec.status(status_blob(), info_blob())
        self.assertEqual(d["clock_comparison"]["exact_matches"][0], [0, 12, 14, 15])
        self.assertIn("do not establish", d["clock_comparison"]["interpretation"])


class Desktop(unittest.TestCase):
    def panel(self):
        return dict(
            window=dict(
                fullscreen=True, geometry=dict(x=0, y=1440, width=2048, height=1280)
            ),
            displays=[
                dict(
                    name="eDP-1",
                    pos=dict(x=0, y=1440),
                    size=dict(width=2560, height=1600),
                    scale=1.25,
                    rotation=1,
                )
            ],
        )

    def test_geometry_not_render_extent(self):
        self.assertEqual(series.panel_geometry(self.panel())["width"], 2048)
        self.assertEqual(
            witness.geometry(
                "Absolute upper-left X: -2\nAbsolute upper-left Y: 10\nWidth: 200\nHeight: 100\n"
            )["x"],
            -2,
        )
        with self.assertRaises(run.Stop):
            witness.geometry("Width: 200")

    def test_external_or_moved_window_rejected(self):
        p = self.panel()
        p["window"]["geometry"]["y"] = 0
        with self.assertRaises(run.Stop):
            series.panel_geometry(p)
        p = self.panel()
        p["window"]["fullscreen"] = False
        with self.assertRaises(run.Stop):
            series.panel_geometry(p)

    def test_window_pid_must_belong_to_workload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            replies = [
                (0, '0x123 "FurMark" 100x100+0+0'),
                (
                    0,
                    "_NET_WM_PID(CARDINAL) = 999\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN",
                ),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(run, "process_table", return_value={100: (1, "x")}),
            ):
                with self.assertRaisesRegex(run.Stop, "owned"):
                    witness.capture(root / "capture", 100)

    def test_owned_window_geometry_saved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = dict(
                outputs=[
                    dict(
                        connected=True,
                        enabled=True,
                        currentModeId="1",
                        modes=[
                            dict(
                                id="1",
                                size=dict(width=2560, height=1600),
                                refreshRate=240,
                            )
                        ],
                        name="eDP-1",
                        pos=dict(x=0, y=1440),
                        scale=1.25,
                        rotation=1,
                    )
                ]
            )
            replies = [
                (0, '0x123 "FurMark" 2048x1280+0+1440'),
                (
                    0,
                    "_NET_WM_PID(CARDINAL) = 101\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN",
                ),
                (
                    0,
                    "Absolute upper-left X: 0\nAbsolute upper-left Y: 1440\nWidth: 2048\nHeight: 1280\nMap State: IsViewable",
                ),
                (0, json.dumps(config)),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(
                    run, "process_table", return_value={100: (1, "x"), 101: (100, "y")}
                ),
            ):
                result = witness.capture(root / "capture", 100)
            self.assertEqual(series.panel_geometry(result)["width"], 2048)
            self.assertTrue((root / "capture/result.json").exists())

    def test_untitled_owned_window_among_unrelated_windows(self):
        # Observed shape: the visible candidate is '(has no name)'.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tree = (
                "xwininfo: Window id: 0x445 (the root window)\n"
                '  0x1000001 "xsettingsd": () 1x1+-1+-1\n'
                "  0xe00002 (has no name): () 2048x1280+1477+1440\n"
            )
            replies = [
                (0, tree),
                (0, "_NET_WM_PID(CARDINAL) = 999"),
                (
                    0,
                    "_NET_WM_PID(CARDINAL) = 101\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN",
                ),
                (
                    0,
                    "Absolute upper-left X: 1477\nAbsolute upper-left Y: 1440\nWidth: 2048\nHeight: 1280\nMap State: IsViewable",
                ),
                (0, json.dumps(dict(outputs=[]))),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(
                    run, "process_table", return_value={100: (1, "x"), 101: (100, "y")}
                ),
            ):
                result = witness.capture(root / "capture", 100)
            self.assertEqual(result["window"]["window"], "0xe00002")
            self.assertEqual(result["window"]["geometry"]["x"], 1477)

    def test_missing_pid_uses_server_owner_not_title_or_size(self):
        with tempfile.TemporaryDirectory() as td:
            replies = [
                (0, "0xe00002 (has no name): () 2048x1280+0+0"),
                (
                    0,
                    "_NET_WM_PID: not found.\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN",
                ),
                (0, json.dumps(dict(owners={"0xe00002": 101}))),
                (
                    0,
                    "Absolute upper-left X: 0\nAbsolute upper-left Y: 0\nWidth: 2048\nHeight: 1280\nMap State: IsViewable",
                ),
                (0, json.dumps(dict(outputs=[]))),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(
                    run, "process_table", return_value={100: (1, "x"), 101: (100, "y")}
                ),
            ):
                result = witness.capture(Path(td) / "capture", 100)
            self.assertEqual(result["window"]["pid_source"], "XRes-local-client")

    def test_missing_pid_unowned_server_reply_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            replies = [
                (0, '0x12 "FurMark": ()'),
                (0, "_NET_WM_PID: not found."),
                (0, json.dumps(dict(owners={"0x12": 999}))),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(run, "process_table", return_value={100: (1, "x")}),
            ):
                with self.assertRaisesRegex(run.Stop, "found 0"):
                    witness.capture(Path(td) / "capture", 100)

    def test_vanished_unrelated_window_cannot_supply_ownership(self):
        with tempfile.TemporaryDirectory() as td:
            replies = [(0, "0x12 (has no name): () 1x1+0+0"), (1, "BadWindow")]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(run, "process_table", return_value={100: (1, "x")}),
            ):
                with self.assertRaisesRegex(run.Stop, "found 0 among 1"):
                    witness.capture(Path(td) / "capture", 100)

    def test_multiple_owned_visible_windows_still_refused(self):
        with tempfile.TemporaryDirectory() as td:
            prop = "_NET_WM_PID(CARDINAL) = 101\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN"
            geom = "Absolute upper-left X: 0\nAbsolute upper-left Y: 0\nWidth: 100\nHeight: 100\nMap State: IsViewable"
            replies = [
                (0, '0x12 (has no name): ()\n0x13 "FurMark": ()'),
                (0, prop),
                (0, geom),
                (0, prop),
                (0, geom),
            ]
            with (
                patch.object(witness, "command", side_effect=replies),
                patch.object(
                    run, "process_table", return_value={100: (1, "x"), 101: (100, "y")}
                ),
            ):
                with self.assertRaisesRegex(run.Stop, "found 2"):
                    witness.capture(Path(td) / "capture", 100)

    def test_atomic_witness_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "witness"
            out.mkdir()
            run.check_witness_result(root)  # not published yet
            witness.publish_result(
                out, dict(complete=False, error="ownership census failed")
            )
            self.assertFalse((out / "result.pending.json").exists())
            with self.assertRaisesRegex(
                run.Stop, "desktop witness failed: ownership census failed"
            ):
                run.check_witness_result(root)
            witness.publish_result(out, dict(complete=True))
            run.check_witness_result(root)
            (out / "result.json").write_text("{")
            with self.assertRaisesRegex(run.Stop, "result invalid"):
                run.check_witness_result(root)

    def test_config_ignores_id_not_refresh(self):
        d = dict(
            outputs=[
                dict(
                    connected=True,
                    enabled=True,
                    currentModeId="1",
                    modes=[
                        dict(id="1", size=dict(width=100, height=100), refreshRate=60)
                    ],
                    name="eDP-1",
                    pos=dict(x=0, y=0),
                    scale=1,
                    rotation=1,
                )
            ]
        )
        e = copy.deepcopy(d)
        e["outputs"][0]["currentModeId"] = "99"
        e["outputs"][0]["modes"][0]["id"] = "99"
        self.assertEqual(witness.display_config(d), witness.display_config(e))
        e["outputs"][0]["modes"][0]["refreshRate"] = 144
        self.assertNotEqual(witness.display_config(d), witness.display_config(e))

    def test_xid_not_all_allocation_errors(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "journal"
            p.write_text("kernel NVRM: allocation NV_ERR_NO_MEMORY")
            self.assertFalse(run.has_xid(p))
            p.write_text("kernel NVRM: Xid (PCI:0000:01:00): 31")
            self.assertTrue(run.has_xid(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
