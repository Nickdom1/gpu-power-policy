import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parents[1] / "src/gpu-power.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def test_module_guard_preserves_nixos_and_rejects_unbooted_fedora_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "version"
            loaded.write_text(cli.VERSION)
            booted = root / "booted"
            gpu = root / "0000:01:00.0"
            gpu.mkdir()
            config = {"modinfo": "/modinfo", "device": gpu.name}
            with patch.object(cli, "BOOTED_MODULES", booted), patch.object(cli, "LOADED_VERSION", loaded), \
                    patch.object(cli, "PROC_GPUS", root), patch.object(cli.os, "geteuid", return_value=0), \
                    patch.object(cli, "command_output",
                                 side_effect=lambda *a: cli.VERSION if a[-2] == "version" else "1") as run:
                # A newly installed module on disk is insufficient while the stock one is loaded.
                with self.assertRaisesRegex(ValueError, "reboot"):
                    cli.guard(config)
                (gpu / "boot_vbios").touch()
                cli.guard(config)
                self.assertNotIn("-b", run.call_args.args)
                self.assertIn(cli.os.uname().release, run.call_args.args)
                booted.mkdir()
                (gpu / "boot_vbios").unlink()
                cli.guard(config)
                self.assertEqual(run.call_args.args[1:3], ("-b", str(booted)))
                loaded.write_text("595.71.05")
                with self.assertRaisesRegex(ValueError, "loaded NVIDIA module"):
                    cli.guard(config)

    def test_arguments_and_bounds(self):
        for command, value in (("target", "125"), ("target", "off"), ("current", "107000"), ("current", "off")):
            args = cli.parser().parse_args([command, value])
            self.assertEqual((args.command, args.value), (command, value))
        self.assertEqual(cli.parser().parse_args(["dump", "/tmp/capture"]).directory, Path("/tmp/capture"))
        self.assertEqual(cli.bounded("125", 1, 140), 125)
        for value in ("0", "141", "-1", "1.5", "nan", "125\n"):
            with self.assertRaises(ValueError):
                cli.bounded(value, 1, 140)

    def test_off_releases_target_and_skips_current_without_staged_image(self):
        self.assertEqual(cli.parser().parse_args(["off"]).command, "off")
        config = {"output_sha256": "x", "currentRatedMilliamps": 99000, "currentCeilingMilliamps": 120000,
                  "powerCeilingWatts": 140, "systemctl": "/systemctl", "nvidiaSmi": "/nvidia-smi",
                  "device": "0000:01:00.0"}
        with patch.object(cli, "image_selection", return_value={"state": "rejected"}), \
                patch.object(cli, "RM") as rm, patch.object(cli, "target", return_value={"target_mw": None}) as off:
            self.assertEqual(cli.run(cli.parser().parse_args(["off"]), config), {"target_mw": None})
            rm.assert_not_called()
            off.assert_called_once_with(config, "off")
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            cli.run(cli.parser().parse_args(["off"]), {"device": "0000:01:00.0"})

    def test_selection_is_per_gpu_and_last_record_wins(self):
        selected = "NVRM: vbios_override: GPU 0000:01:00 selected: 516608 bytes from offset 0x0 staged for GSP"
        rejected = "NVRM: vbios_override: GPU 0000:01:00 rejected override at image-read (0x56); using stock VBIOS"
        self.assertEqual(cli.selection(selected, "0000:01:00.0")["state"], "selected")
        self.assertEqual(cli.selection(selected + "\n" + rejected, "0000:01:00.0")["state"], "rejected")
        self.assertEqual(cli.selection(rejected + "\n" + selected, "0000:01:00.0")["state"], "selected")
        self.assertEqual(cli.selection(selected, "0000:02:00.0")["state"], "unknown")

    def test_target_release_and_bounds(self):
        config = {"powerCeilingWatts": 140, "systemctl": "/systemctl", "nvidiaSmi": "/nvidia-smi",
                  "device": "0000:01:00.0"}
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "RUN", Path(directory)):
            with patch.object(cli.subprocess, "run") as restart, \
                    patch.object(cli, "command_output", return_value="active"):
                self.assertEqual(cli.target(config, "125")["target_mw"], 125000)
                self.assertEqual((Path(directory) / "target-mw").read_text(), "125000\n")
                with self.assertRaises(ValueError):
                    cli.target(config, "141")
                self.assertEqual((Path(directory) / "target-mw").read_text(), "125000\n")
                self.assertIsNone(cli.target(config, "off")["target_mw"])
                self.assertFalse((Path(directory) / "target-mw").exists())
                self.assertEqual(restart.call_count, 2)
                restart.assert_called_with(["/systemctl", "restart", "nvidia-powerd.service"], check=True)

    def test_rom_round_trip_and_refusals(self):
        bdf, payload = "0001:02:03.4", b"\x55\xaa94.03.19.00.01" * 32
        fields = [1, 64, 1, 0, len(payload), 2, 1, 2, 3, 4, 0x10DE, 0x1234, 0, 0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpu = root / bdf
            gpu.mkdir()
            (gpu / "vendor").write_text("0x10de")
            (gpu / "device").write_text("0x1234")
            args = cli.parser().parse_args(["rom", str(root / "gpu.rom")])
            with patch.object(cli, "PROC_GPUS", root), patch.object(cli, "PCI_DEVICES", root), \
                    patch.object(cli, "RM", side_effect=AssertionError("ROM must not open RM")), \
                    patch.object(cli, "image_selection", side_effect=AssertionError("no override required")):
                def snapshot(values, magic=b"NVBROM1\0", image=payload):
                    (gpu / "boot_vbios").write_bytes(magic + cli.struct.pack("<14I", *values) + image)
                snapshot(fields)
                result = cli.run(args, {"device": bdf})
                self.assertEqual(args.output.read_bytes(), payload)
                self.assertEqual(args.output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(result, {"size": len(payload), "sha256": cli.hashlib.sha256(payload).hexdigest(),
                    "expansion_rom_offset": 2, "vbios_version": "94.03.19.00.01"})
                with self.assertRaises(FileExistsError):
                    cli.run(args, {"device": bdf})
                args.output.unlink()
                for state, reason in ((0, "not reached"), (2, "extraction failed"), (3, "invalid image"),
                                      (4, "allocation failed"), (99, "unknown state")):
                    snapshot(fields[:2] + [state, 0, 0, 0] + fields[6:], image=b"")
                    with self.assertRaisesRegex(ValueError, reason):
                        cli.run(args, {"device": bdf})
                for index, value in ((0, 2), (1, 63), (3, 1), (4, 0), (4, 1048577),
                                     (5, len(payload)+1), (6, 0), (10, 0), (11, 0), (12, 1)):
                    bad = fields.copy()
                    bad[index] = value
                    snapshot(bad)
                    with self.assertRaises(ValueError):
                        cli.run(args, {"device": bdf})
                snapshot(fields, magic=b"BADMAGIC")
                with self.assertRaises(ValueError):
                    cli.run(args, {"device": bdf})
                self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
