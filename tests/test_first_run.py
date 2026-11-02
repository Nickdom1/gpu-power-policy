from test_pins import FIXTURES
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import baseline
import policy

spec = importlib.util.spec_from_file_location("first_run_cli", Path(__file__).resolve().parents[1] / "src/gpu-power.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
STOCK = dict(powerCeilingWatts=115, powerDefaultWatts=80, currentCeilingMilliamps=99000, currentRatedMilliamps=99000)
ROM = dict(
    family="ampere",
    pci_device="10de:2460",
    sha256=cli.hashlib.sha256(b"ROM").hexdigest(),
    notebook=True,
    values=STOCK,
    policies={"power": 2, "current": 13},
    editable=dict.fromkeys(STOCK, True),
)


def firmware(values):
    raw = bytearray((FIXTURES / "a618.bin").read_bytes())
    for index, seed, ceiling, scale in (
        (2, "powerDefaultWatts", "powerCeilingWatts", 1000),
        (13, "currentRatedMilliamps", "currentCeilingMilliamps", 1),
    ):
        struct.pack_into("<II", raw, 0xCC + index * 0x104 + 12, values[seed] * scale, values[ceiling] * scale)
    return bytes(raw)


class FirstRunTests(unittest.TestCase):
    def test_defaults_immutable_and_acknowledgement(self):
        self.assertEqual(baseline.propose(ROM, {}), STOCK)
        with self.assertRaisesRegex(ValueError, r"21.7%.*--beyond-tested"):
            baseline.propose(ROM, {"powerCeilingWatts": 140})
        result = baseline.propose(
            ROM, {"powerCeilingWatts": 140, "currentCeilingMilliamps": 120000, "beyondTested": True}
        )
        self.assertEqual([result[k] for k in baseline.SEEDS], [80, 99000])
        readonly = copy.deepcopy(ROM)
        readonly["editable"]["currentCeilingMilliamps"] = False
        with self.assertRaisesRegex(ValueError, "read-only"):
            baseline.propose(readonly, {"currentCeilingMilliamps": 120000, "beyondTested": True})

    def test_stock_and_raised_boot_identity_and_acceptance(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "read_boot_rom", return_value=(b"ROM", {"vbios_version": "test"})),
            patch.object(cli, "captured_baseline", return_value=ROM),
            patch.object(cli, "image_selection", return_value={"state": "selected", "line": "selected"}),
        ):
            config = {"device": "0000:01:00.0"}
            self.assertEqual(cli.live_state(config, firmware(STOCK))["verdict"], "Ready")
            raised = {
                **config,
                **STOCK,
                "currentCeilingMilliamps": 107000,
                "romBaseline": ROM,
                "output_sha256": "b" * 64,
            }
            state = cli.live_state(raised, firmware({**STOCK, "currentCeilingMilliamps": 107000}))
            self.assertEqual(state["verdict"], "Ready")
            state = cli.live_state(raised, firmware(STOCK))
            self.assertEqual(state["verdict"], "Unverified")
            with self.assertRaisesRegex(ValueError, "Unverified"):
                cli.require_image(raised, state)
            stale = {**raised, "romBaseline": {**ROM, "sha256": "0" * 64}}
            self.assertEqual(cli.live_state(stale, firmware(STOCK))["verdict"], "Stale")
            swapped = {**raised, "romBaseline": {**ROM, "pci_device": "10de:1234"}}
            self.assertEqual(cli.live_state(swapped, firmware(STOCK))["verdict"], "Stale")

    def test_configure_stock_stages_nothing_and_raise_works_on_raised_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pci = root / "0000:01:00.0"
            pci.mkdir()
            (pci / "vendor").write_text("0x10de")
            (pci / "device").write_text("0x2460")
            args = cli.parser().parse_args(["configure", "--json"])
            config = {"device": pci.name}
            with (
                patch.object(cli, "PCI_DEVICES", root),
                patch.object(cli, "ROM_STORE", root / "saved"),
                patch.object(cli, "read_boot_rom", return_value=(b"ROM", {})),
                patch.object(baseline, "read_rom", return_value=ROM),
                patch.object(cli, "RM"),
                patch.object(policy, "query", return_value=firmware(STOCK)) as query,
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(cli, "current", side_effect=AssertionError("must not SET")),
            ):
                result = json.loads(cli.configure(args, config))
                self.assertIsNone(result["vbios"])
                self.assertFalse((root / "saved").exists())
                args.current_ceiling = "+8%"
                args.boot_power = "999999"
                with self.assertRaisesRegex(ValueError, "--boot-power"):
                    cli.configure(args, config)
                self.assertFalse((root / "saved").exists())
                args.boot_power = None
                result = json.loads(cli.configure(args, config))
                self.assertEqual(result["currentCeilingMilliamps"], 107000)
                saved = Path(result["vbios"])
                self.assertEqual(saved.stat().st_mode & 0o777, 0o644)
                self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o755)
                query.return_value = firmware({**STOCK, "currentCeilingMilliamps": 107000})
                self.assertEqual(json.loads(cli.configure(args, config)), result)
                args.nix = True
                module = cli.configure(args, config)
                self.assertTrue(module.startswith("{\n  hardware.gpuPowerPolicy = {\n"))
                self.assertTrue(module.endswith("  };\n}"))
                self.assertIn("currentCeilingMilliamps = 107000;", module)
                self.assertNotIn("enable =", module)  # The host owns enable/disable.
                saved.write_bytes(b"different")
                with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                    cli.configure(args, config)

    def test_cap_percentages_and_ada_refusals(self):
        self.assertEqual(baseline.propose(ROM, {"currentCeilingMilliamps": "+8%"})["currentCeilingMilliamps"], 107000)
        with self.assertRaisesRegex(ValueError, "cap"):
            baseline.propose(ROM, {"currentCeilingMilliamps": "+15%"})
        ada = {**ROM, "family": "ada"}
        with self.assertRaisesRegex(ValueError, "read-only"):
            baseline.propose(ada, {"currentCeilingMilliamps": 99000})
        with self.assertRaisesRegex(ValueError, "Ada laptop"):
            baseline.propose(ada, {"powerCeilingWatts": "+5%"})

    def test_null_device_selects_one_gpu_and_malformed_baseline_refuses(self):
        config = dict.fromkeys(["modinfo", "systemctl", "journalctl", "dmesg", "nvidiaSmi"], "/tool")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpu = root / "0000:01:00.0"
            gpu.mkdir()
            (gpu / "vendor").write_text("0x10de")
            (gpu / "class").write_text("0x030200")
            with patch.object(cli, "PCI_DEVICES", root):
                self.assertEqual(cli.validate_config({**config, "device": None})["device"], gpu.name)
                other = root / "0000:02:00.0"
                other.mkdir()
                (other / "vendor").write_text("0x10de")
                (other / "class").write_text("0x030000")
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    cli.validate_config({**config, "device": None})
        with self.assertRaisesRegex(ValueError, "missing required"):
            baseline.compare({"family": "ampere"}, {}, firmware(STOCK))
        with self.assertRaisesRegex(ValueError, "runtime boot defaults"):
            cli.validate_config(
                {
                    **config,
                    **STOCK,
                    "device": "0000:01:00.0",
                    "powerDefaultWatts": 81,
                    "output_size": 1,
                    "output_sha256": "a" * 64,
                    "romBaseline": ROM,
                }
            )

    def test_blackwell_and_fractional_defaults_refuse(self):
        with patch.object(baseline, "editor", return_value={"architecture": {"family": "blackwell"}}):
            with self.assertRaisesRegex(ValueError, "blackwell"):
                baseline.read_rom("unused")
        with self.assertRaisesRegex(ValueError, "refusing to round"):
            baseline.whole(80.5, "power_default_w")

    def test_stock_comparison_and_changed_ceilings_are_distinct(self):
        stock = baseline.compare(ROM, {}, firmware(STOCK))
        self.assertTrue(stock["match"] and stock["seeds_match"])
        raised = {**STOCK, "powerCeilingWatts": 140, "currentCeilingMilliamps": 120000}
        result = baseline.compare(ROM, raised, firmware(raised))
        self.assertTrue(result["match"] and result["seeds_match"])
        self.assertNotEqual(result["rom"], result["live"])
        with self.assertRaisesRegex(ValueError, "baseline mismatch"):
            baseline.require_match(baseline.compare(ROM, {}, firmware(raised)))


if __name__ == "__main__":
    unittest.main()
