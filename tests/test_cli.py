import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parents[1] / "src/gpu-power.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def test_runtime_config_missing_keys_fail_before_hardware(self):
        config = {key: "/tool" for key in ("modinfo", "systemctl", "journalctl", "dmesg", "nvidiaSmi")}
        config["device"] = "0000:01:00.0"
        self.assertEqual(cli.validate_config(config), config)
        for missing in config:
            broken = {key: value for key, value in config.items() if key != missing}
            with (
                patch.object(cli.CONFIG.__class__, "read_text", return_value=cli.json.dumps(broken)),
                patch.object(cli.sys, "argv", ["gpu-power", "status"]),
                patch.object(cli, "guard", side_effect=AssertionError("must validate first")),
                self.assertRaisesRegex(SystemExit, "missing required key: " + missing),
            ):
                cli.main()
        with self.assertRaisesRegex(ValueError, "powerDefaultWatts"):
            cli.validate_config({**config, "powerCeilingWatts": 140})

    def test_current_off_with_lost_log_only_lowers_live_current(self):
        config = {"device": "0000:01:00.0", "currentRatedMilliamps": 99000,
                  "currentCeilingMilliamps": 120000, "output_sha256": "x"}
        fixtures = Path(__file__).parent / "fixtures/615.71.09"
        info = (fixtures / "a618.bin").read_bytes()
        full = (fixtures / "a61a.bin").read_bytes()
        single = (fixtures / "a61a-current.bin").read_bytes()

        class Client:
            current_ma = 107000
            writes = 0

            def control(self, command, raw):
                if command == cli.policy.INFO:
                    return 0, info
                if command == cli.policy.SET:
                    self.current_ma = cli.policy.u32(raw, 0xA0C)
                    self.writes += 1
                reply = bytearray(full if cli.policy.u32(raw, 0x10) == cli.policy.u32(info, 4) else single)
                cli.struct.pack_into("<I", reply, 0xA0C, self.current_ma)
                return 0, reply

        client = Client()
        with (
            patch.object(cli, "image_selection", return_value={"state": "unknown"}) as selection,
            patch.object(cli, "RM") as rm,
            patch.object(cli, "target", return_value={}),
            patch.object(cli, "limit", return_value={}),
        ):
            rm.return_value.__enter__.return_value = client
            self.assertTrue(cli.release(config)["steps"]["current"]["ok"])
            self.assertEqual((client.current_ma, client.writes), (99000, 1))
            for value in (99000, 98000):
                client.current_ma = value
                self.assertEqual(cli.current(config, "off"), {"current_ma": value})
            self.assertEqual(client.writes, 1)
            selection.assert_not_called()
            with self.assertRaisesRegex(ValueError, "not selected"):
                cli.current(config, "107000")
            self.assertEqual(client.writes, 1)

    def test_nvml_uses_real_driver_status_and_milliwatts(self):
        nvml = Mock()
        nvml.nvmlInit_v2.return_value = 0
        nvml.nvmlDeviceGetHandleByPciBusId_v2.side_effect = lambda bdf, ptr: setattr(ptr._obj, "value", 1234) or 0
        nvml.nvmlDeviceSetPowerManagementLimit.return_value = 3
        nvml.nvmlErrorString.return_value = b"Not Supported"
        with patch.object(cli.ctypes, "CDLL", return_value=nvml):
            self.assertEqual(
                cli.standard_limit({"device": "0000:01:00.0"}, 125), {"status": 3, "message": "Not Supported"}
            )
        handle, milliwatts = nvml.nvmlDeviceSetPowerManagementLimit.call_args.args
        self.assertEqual((handle.value, milliwatts), (1234, 125000))
        nvml.nvmlShutdown.assert_called_once_with()

    def test_target_restart_failure_restores_set_but_keeps_off_removed(self):
        config = {"systemctl": "/systemctl", "powerCeilingWatts": 140}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "require_image"),
            patch.object(cli, "service_state", return_value="active"),
            patch.object(cli.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "restart")),
        ):
            path = Path(directory) / "target-mw"
            for previous in (None, b"125000"):
                if previous is not None:
                    path.write_bytes(previous)
                with self.assertRaisesRegex(RuntimeError, "previous target restored"):
                    cli.target(config, "125")
                self.assertEqual(path.read_bytes() if path.exists() else None, previous)
            with self.assertRaisesRegex(RuntimeError, "target remains removed"):
                cli.target(config, "off")
            self.assertFalse(path.exists())

    def test_target_without_powerd_is_noop(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "service_state", return_value="inactive"),
            patch.object(cli, "require_image", side_effect=AssertionError("must not mutate")),
        ):
            path = Path(directory) / "target-mw"
            path.write_bytes(b"125000\n")
            result = cli.target({}, "125")
            self.assertEqual(result["powerd"], "inactive")
            self.assertFalse(result["changed"])
            self.assertEqual(path.read_bytes(), b"125000\n")
            cli.target({}, "off")
            self.assertFalse(path.exists())

    def test_limit_packing_bounds_and_fallback_order(self):
        config = {"device": "0000:01:00.0", "nvidiaSmi": "/smi", "powerCeilingWatts": 140, "powerDefaultWatts": 80}
        events = []

        def standard(*args, **kwargs):
            events.append("standard")
            return {"status": 3, "message": "Not Supported"}

        with (
            patch.object(cli, "require_image"),
            patch.object(cli, "power_readback", side_effect=[{"enforced_watts": 80}, {"enforced_watts": 125}]),
            patch.object(cli, "standard_limit", side_effect=standard),
            patch.object(cli, "RM") as rm,
        ):
            control = rm.return_value.__enter__.return_value.control
            control.side_effect = lambda *_: events.append("role-8")
            result = cli.limit(config, "125")
            self.assertEqual(events, ["standard", "role-8"])
            control.assert_called_once_with(0x2080E61E, bytes.fromhex("08fe000048e8010000000000"))
            self.assertEqual(result["path"], "role-8")
            self.assertTrue(result["changed"])
        with patch.object(cli, "require_image"), patch.object(cli, "power_readback") as readback:
            for bad in ("0", "141", "-1", "1.5"):
                with self.assertRaises(ValueError):
                    cli.limit(config, bad)
            readback.assert_not_called()
        with patch.object(cli, "image_selection", return_value={"state": "rejected"}):
            with self.assertRaisesRegex(ValueError, "not selected"):
                cli.limit({**config, "output_sha256": "x"}, "125")

    def test_limit_standard_success_does_not_fallback_when_unchanged(self):
        config = {"device": "0000:01:00.0", "nvidiaSmi": "/smi", "powerCeilingWatts": 140, "powerDefaultWatts": 80}
        with (
            patch.object(cli, "require_image"),
            patch.object(cli, "power_readback", return_value={"enforced_watts": 80}),
            patch.object(cli, "standard_limit", return_value={"status": 0, "message": "Success"}) as run,
            patch.object(cli, "RM") as rm,
        ):
            result = cli.limit(config, "off")
            self.assertEqual(result["path"], "standard")
            self.assertEqual(result["requested_watts"], 80)
            self.assertFalse(result["changed"])
            self.assertIn("did not move", result["message"])
            run.assert_called_once_with(config, 80)
            rm.assert_not_called()

    def test_boot_initializer_staging_gate_and_both_paths(self):
        config = {"powerdTargetWatts": 125, "powerCeilingWatts": 140}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "image_selection", return_value={"state": "rejected"}) as selection,
            patch.object(cli, "dynamic_boost", return_value=True) as boost,
            patch.object(cli, "service_state", return_value="enabled"),
            patch.object(cli, "limit", return_value={"path": "role-8"}) as limit,
        ):
            path = Path(directory) / "target-mw"
            path.write_text("125000\n")
            self.assertEqual(cli.boot_init(config)["boot"], "skipped")
            self.assertEqual(path.read_text(), "125000\n")
            limit.assert_not_called()
            boost.assert_not_called()
            selection.return_value = {"state": "selected"}
            self.assertEqual(cli.boot_init(config)["boot"], "target")
            self.assertEqual(path.read_text(), "125000\n")
            boost.return_value = False
            self.assertEqual(cli.boot_init(config), {"boot": "limit", "path": "role-8"})
            limit.assert_called_once_with(config, "125")
            self.assertFalse(path.exists())

    def test_module_guard_preserves_nixos_and_rejects_unbooted_fedora_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "version"
            loaded.write_text(cli.VERSION)
            booted = root / "booted"
            gpu = root / "0000:01:00.0"
            gpu.mkdir()
            config = {"modinfo": "/modinfo", "device": gpu.name}
            with (
                patch.object(cli, "BOOTED_MODULES", booted),
                patch.object(cli, "LOADED_VERSION", loaded),
                patch.object(cli, "PROC_GPUS", root),
                patch.object(cli.os, "geteuid", return_value=0),
                patch.object(
                    cli, "command_output", side_effect=lambda *a: cli.VERSION if a[-2] == "version" else "1"
                ) as run,
            ):
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

    def test_off_attempts_all_steps_and_reports_failures(self):
        with (
            patch.object(cli, "guard"),
            patch.object(cli, "current", side_effect=RuntimeError("current failed")) as current,
            patch.object(cli, "target", side_effect=RuntimeError("restart failed")) as target,
            patch.object(cli, "limit", return_value={"requested_watts": 80}) as limit,
        ):
            result = cli.run(cli.parser().parse_args(["off"]), {})
            self.assertFalse(result["ok"])
            self.assertFalse(result["steps"]["current"]["ok"])
            self.assertFalse(result["steps"]["target"]["ok"])
            self.assertTrue(result["steps"]["limit"]["ok"])
            for action in (current, target, limit):
                action.assert_called_once_with({}, "off")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli.CONFIG.__class__, "read_text", return_value="{}"),
            patch.object(cli, "validate_config", return_value={}),
            patch.object(cli.sys, "argv", ["gpu-power", "off"]),
            patch.object(cli, "run", return_value=result),
            patch("builtins.print"),
        ):
            with self.assertRaises(SystemExit) as exit:
                cli.main()
            self.assertEqual(exit.exception.code, 1)

    def test_selection_is_per_gpu_and_last_record_wins(self):
        selected = "NVRM: vbios_override: GPU 0000:01:00 selected: 516608 bytes from offset 0x0 staged for GSP"
        rejected = "NVRM: vbios_override: GPU 0000:01:00 rejected override at image-read (0x56); using stock VBIOS"
        self.assertEqual(cli.selection(selected, "0000:01:00.0")["state"], "selected")
        self.assertEqual(cli.selection(selected + "\n" + rejected, "0000:01:00.0")["state"], "rejected")
        self.assertEqual(cli.selection(rejected + "\n" + selected, "0000:01:00.0")["state"], "selected")
        self.assertEqual(cli.selection(selected, "0000:02:00.0")["state"], "unknown")
        self.assertEqual(cli.selection(selected, "0000:01:00.1")["state"], "unknown")
        full = selected.replace("0000:01:00 ", "0000:01:00.1 ")
        self.assertEqual(cli.selection(full, "0000:01:00.1")["state"], "selected")
        self.assertEqual(cli.selection(full, "0000:01:00.0")["state"], "unknown")

    def test_target_release_and_bounds(self):
        config = {
            "output_sha256": "x",
            "powerCeilingWatts": 140,
            "systemctl": "/systemctl",
            "nvidiaSmi": "/nvidia-smi",
            "device": "0000:01:00.0",
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "RUN", Path(directory)):
            with (
                patch.object(cli.subprocess, "run") as restart,
                patch.object(cli, "service_state", return_value="active"),
                patch.object(cli, "image_selection", return_value={"state": "selected"}),
                patch.object(cli, "command_output", return_value="power readback"),
            ):
                self.assertEqual(cli.target(config, "125")["target_mw"], 125000)
                self.assertEqual((Path(directory) / "target-mw").read_text(), "125000\n")
                with self.assertRaises(ValueError):
                    cli.target(config, "141")
                self.assertEqual((Path(directory) / "target-mw").read_text(), "125000\n")
                self.assertIsNone(cli.target(config, "off")["target_mw"])
                self.assertFalse((Path(directory) / "target-mw").exists())
                self.assertEqual(restart.call_count, 2)
                restart.assert_called_with(
                    ["/systemctl", "restart", "nvidia-powerd.service"], check=True, capture_output=True, text=True
                )

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
            with (
                patch.object(cli, "PROC_GPUS", root),
                patch.object(cli, "PCI_DEVICES", root),
                patch.object(cli, "RM", side_effect=AssertionError("ROM must not open RM")),
                patch.object(cli, "image_selection", side_effect=AssertionError("no override required")),
            ):

                def snapshot(values, magic=b"NVBROM1\0", image=payload):
                    (gpu / "boot_vbios").write_bytes(magic + cli.struct.pack("<14I", *values) + image)

                snapshot(fields)
                result = cli.run(args, {"device": bdf})
                self.assertEqual(args.output.read_bytes(), payload)
                self.assertEqual(args.output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    result,
                    {
                        "size": len(payload),
                        "sha256": cli.hashlib.sha256(payload).hexdigest(),
                        "expansion_rom_offset": 2,
                        "vbios_version": "94.03.19.00.01",
                    },
                )
                with self.assertRaises(FileExistsError):
                    cli.run(args, {"device": bdf})
                args.output.unlink()
                for state, reason in (
                    (0, "not reached"),
                    (2, "extraction failed"),
                    (3, "invalid image"),
                    (4, "allocation failed"),
                    (99, "unknown state"),
                ):
                    snapshot(fields[:2] + [state, 0, 0, 0] + fields[6:], image=b"")
                    with self.assertRaisesRegex(ValueError, reason):
                        cli.run(args, {"device": bdf})
                for index, value in (
                    (0, 2),
                    (1, 63),
                    (3, 1),
                    (4, 0),
                    (4, 1048577),
                    (5, len(payload) + 1),
                    (6, 0),
                    (10, 0),
                    (11, 0),
                    (12, 1),
                ):
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
