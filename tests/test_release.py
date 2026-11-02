"""Release safety regressions: real policy decoding, mocked OS boundaries, no GPU writes."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import shlex
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_pins import FIXTURES
from test_cli import cli, ready
from test_first_run import ROM, STOCK, firmware

spec = importlib.util.spec_from_file_location("preflight", Path(__file__).parents[1] / "install/preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class RuntimeSafety(unittest.TestCase):
    def test_malformed_baseline_reports_repair_before_hardware(self):
        for key in ("pci_device", "sha256"):
            for bad_value in (None, "invalid", []):
                config = {
                    "device": "0000:01:00.0", **STOCK, "romBaseline": {**ROM, key: bad_value},
                    "output_size": 1, "output_sha256": "a" * 64,
                }
                with (
                    self.subTest(key=key, value=bad_value),
                    patch.object(cli.os, "geteuid", return_value=0),
                    patch.object(cli.CONFIG.__class__, "read_text", return_value=json.dumps(config)),
                    patch.object(cli, "guard") as guard,
                    patch("sys.argv", ["gpu-power", "status"]),
                    redirect_stderr(io.StringIO()) as output,
                    self.assertRaises(SystemExit) as error,
                ):
                    cli.main()
                self.assertEqual(error.exception.code, 1)
                self.assertIn(f"Unverified: ROM baseline missing or invalid {key}", output.getvalue())
                self.assertIn("Run: ", output.getvalue())
                guard.assert_not_called()

    def test_release_tree_contains_no_research_size_guard_escapes(self):
        root = Path(__file__).parents[1]
        for directory in ("src", "install"):
            for path in (root / directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"GPU_POWER_TEST_", path.read_bytes(), str(path.relative_to(root)))

    def test_platform_selection_and_main_repair_messages(self):
        for distro in ("nixos", "fedora", "ubuntu", "cachyos"):
            platform = "nixos" if distro == "nixos" else "linux"
            module_spec = importlib.util.spec_from_file_location("repair_cli", Path(cli.__file__))
            module = importlib.util.module_from_spec(module_spec)
            with patch.object(cli.platform, "freedesktop_os_release", return_value={"ID": distro}):
                module_spec.loader.exec_module(module)
            self.assertEqual(module.REPAIR, module.REPAIRS[platform])
            for command, guard_error, expected in (
                ("status", ValueError("driver absent"), "reinstall"),
                ("status", None, "disable"),
                ("boot-init", None, "blacklist"),
            ):
                with self.subTest(distro=distro, command=command, expected=expected):
                    with (
                        tempfile.TemporaryDirectory() as directory,
                        patch.object(module, "RUN", Path(directory)),
                        patch.object(module.os, "geteuid", return_value=0),
                        patch.object(module.CONFIG.__class__, "read_text", return_value="{}"),
                        patch.object(module, "validate_config", return_value={}),
                        patch.object(module, "guard", side_effect=guard_error),
                        patch.object(module, "run", side_effect=ValueError("bad state")),
                        patch.object(module, "nvidia_blacklisted", return_value=(command == "boot-init")),
                        patch("sys.argv", ["gpu-power", command]),
                        redirect_stderr(io.StringIO()) as output,
                        self.assertRaises(SystemExit) as error,
                    ):
                        module.main()
                    self.assertEqual(error.exception.code, 1)
                    self.assertIn("Run: " + module.REPAIR[expected], output.getvalue())
                    if platform == "nixos":
                        self.assertNotIn("install/linux.sh", output.getvalue())
                        self.assertNotIn("install/preflight.sh", output.getvalue())

    def test_blacklisted_status_and_commands_never_probe_or_load_the_driver(self):
        for command in (["status"], ["status", "--json"], ["boot-init"], ["current", "max"]):
            with (
                self.subTest(command=command), tempfile.TemporaryDirectory() as directory,
                patch.object(cli, "RUN", Path(directory)),
                patch.object(cli.os, "geteuid", return_value=0),
                patch.object(cli.CONFIG.__class__, "read_text", return_value='{"device":"0000:01:00.0"}'),
                patch.object(cli, "nvidia_blacklisted", return_value=True),
                patch.object(cli, "guard") as guard,
                patch.object(cli, "command_output") as probe,
                patch.object(cli, "RM") as rm,
                patch("sys.argv", ["gpu-power", *command]),
                redirect_stderr(io.StringIO()) as error,
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit) as status,
            ):
                cli.main()
            self.assertEqual(status.exception.code, 1)
            message = error.getvalue() + output.getvalue()
            self.assertIn("Missing: NVIDIA is blacklisted", message)
            self.assertIn("Run: ", message)
            if "--json" in command:
                self.assertEqual(json.loads(output.getvalue())["verdict"], "Missing")
            guard.assert_not_called()
            probe.assert_not_called()
            rm.assert_not_called()

    def test_repairs_use_existing_checkout_and_quote_shell_characters(self):
        with tempfile.TemporaryDirectory(prefix="checkout ' $() ") as directory:
            checkout = Path(directory)
            (checkout / "install").mkdir()
            script = checkout / "install/linux.sh"
            script.touch()
            config = {"installerCheckout": directory}
            saved = checkout / "runtime.json"
            saved.write_text(json.dumps(config))
            for exists in (True, False):
                if not exists:
                    script.unlink()
                with self.subTest(exists=exists):
                    repairs = cli.repair_commands(config, "linux")
                    installer = str(script) if exists else "install/linux.sh"
                    self.assertEqual(repairs["reinstall"], f"sudo bash {shlex.quote(installer)}, then reboot")
                    self.assertEqual(shlex.split(repairs["disable"].split(", then")[0]),
                                     ["sudo", "bash", installer, "--disable"])
                    self.assertEqual(cli.repair_commands(config, "nixos"), cli.REPAIRS["nixos"])
                    hook = subprocess.run([
                        "bash", "-c", 'source "$1"; repair_installer "$2"', "bash",
                        str(Path(__file__).parents[1] / "install/lib.sh"), str(saved),
                    ], capture_output=True, text=True, check=True)
                    self.assertEqual(hook.stdout.strip(), "sudo bash " + shlex.quote(installer))

    def test_invalid_checkout_metadata_keeps_repair_available(self):
        for config in (None, [], {"installerCheckout": []}, {"installerCheckout": 42}):
            with self.subTest(config=config):
                self.assertEqual(cli.repair_commands(config, "linux"), cli.REPAIRS["linux"])
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "runtime.json"
            for text in ("{", "null", '{"installerCheckout": []}'):
                saved.write_text(text)
                result = subprocess.run([
                    "bash", "-c", 'source "$1"; repair_installer "$2"', "bash",
                    str(Path(__file__).parents[1] / "install/lib.sh"), str(saved),
                ], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "sudo bash install/linux.sh")

    def test_live_state_repairs_follow_platform(self):
        raised = {"device": "0000:01:00.0", **STOCK, "romBaseline": ROM, "output_sha256": "b" * 64}
        for platform, repair in cli.REPAIRS.items():
            for config, selection, verdict, fix in (
                ({**raised, "romBaseline": {**ROM, "sha256": "0" * 64}}, "selected", "Stale", "recapture"),
                ({**raised, "romBaseline": {**ROM, "pci_device": "10de:1234"}}, "selected", "Stale", "disable"),
                (raised, "rejected", "Unverified", "disable"),
                (raised, "unknown", "Unverified", "disable"),
            ):
                with (
                    self.subTest(platform=platform, verdict=verdict, fix=fix),
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(cli, "RUN", Path(directory)),
                    patch.object(cli, "REPAIR", repair),
                    patch.object(cli, "read_boot_rom", return_value=(b"ROM", {"vbios_version": "test"})),
                    patch.object(cli, "captured_baseline", return_value=ROM),
                    patch.object(cli, "image_selection", return_value={"state": selection, "line": selection}),
                ):
                    result = cli.live_state(config, firmware(STOCK))
                    self.assertEqual(result["verdict"], verdict)
                    self.assertIn("Run: " + repair[fix], result["message"])
                    if platform == "nixos":
                        self.assertNotIn("install/linux.sh", result["message"])

    def test_recovery_blacklist_blocks_boot_probes(self):
        for flag in ("modprobe.blacklist", "rd.driver.blacklist", "module_blacklist"):
            self.assertTrue(cli.nvidia_blacklisted(f"quiet {flag}=other,nvidia,nvidia_drm 3"))
            self.assertFalse(cli.nvidia_blacklisted(f"quiet {flag}=nouveau 3"))
        self.assertFalse(cli.nvidia_blacklisted("quiet nvidia.NVreg_RegistryDwords=RmLinuxVbiosOverrideDisable=1"))

    def test_only_not_supported_may_fall_back_to_firmware(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "live_state", return_value=ready()),
            patch.object(cli, "power_readback", return_value={"enforced_watts": 80}),
            patch.object(cli, "standard_limit") as nvml,
            patch.object(cli, "RM") as rm,
        ):
            for status in (1, 2, 4, 6, 999):
                nvml.return_value = {"status": status, "message": "failed"}
                with self.assertRaisesRegex(RuntimeError, "NVML refused"):
                    cli.limit({"device": "0000:01:00.0"}, "max")
                rm.assert_not_called()
                self.assertFalse((Path(directory) / "limit-set").exists())

    def test_dynamic_boost_off_never_sends_a_limit_without_marker(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "current", return_value={}),
            patch.object(cli, "target", return_value={}),
            patch.object(cli, "limit", return_value={}) as limit,
        ):
            self.assertTrue(cli.release({})["ok"])
            limit.assert_not_called()
            (Path(directory) / "limit-set").write_text("1")
            self.assertTrue(cli.release({})["ok"])
            limit.assert_called_once_with({}, "off")

    def test_stock_ceiling_and_config_are_both_bounds(self):
        state = ready()
        state["rom"]["values"].update(STOCK)
        state["bounds"]["power"][2] = 115000
        state["bounds"]["current"][2] = 99000
        self.assertEqual(cli.runtime_value({}, "power", "max", state), 115)
        self.assertEqual(cli.runtime_value({}, "current", "max", state), 99000)
        with self.assertRaises(ValueError):
            cli.runtime_value({"powerCeilingWatts": 140}, "power", "116", state)
        self.assertEqual(cli.runtime_value({"powerCeilingWatts": 110}, "power", "max", state), 110)

    def test_boot_stock_writes_live_bounds_and_no_target(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "live_state", return_value=ready()),
            patch.object(cli, "limit") as limit,
        ):
            result = cli.boot_init({"device": "0000:01:00.0"})
            self.assertIsNone(result["target_mw"])
            self.assertEqual((Path(directory) / "ceiling-mw").read_text(), "140000\n")
            self.assertEqual(json.loads((Path(directory) / "live.json").read_text())["rom"]["policies"]["current"], 13)
            limit.assert_not_called()

    def test_cache_cannot_hide_changed_rom(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "RUN", Path(directory)),
            patch.object(cli, "image_selection", return_value={"state": "selected", "line": "selected"}),
            patch.object(cli, "read_boot_rom", return_value=(b"new ROM", {})),
            patch.object(cli, "captured_baseline", return_value={**ROM, "sha256": "b" * 64}) as inspect,
        ):
            cached = ready()
            cached["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            (Path(directory) / "live.json").write_text(json.dumps(cached))
            state = cli.live_state(
                {"device": cached["device"], **STOCK, "romBaseline": ROM, "output_sha256": "c" * 64}, firmware(STOCK)
            )
            self.assertEqual(state["verdict"], "Stale")
            inspect.assert_not_called()

    def test_index_disambiguates_equal_rated_policies(self):
        info = bytearray(firmware(STOCK))
        index = 14
        off = 0xCC + index * 0x104
        cli.struct.pack_into("<I", info, 4, cli.policy.u32(info, 4) | (1 << index))
        info[off + 4] = 15
        cli.struct.pack_into("<4I", info, off + 8, 1, 99000, 99000, 0)
        control = (FIXTURES / "a61a.bin").read_bytes()
        self.assertEqual(cli.policy.current_policy(info, control, 99000, 99000, index=13)[0], 13)


class PreflightTests(unittest.TestCase):
    def test_text_prints_every_blocker_and_json_keeps_them(self):
        blockers = [
            "Blocked: a packaged NVIDIA driver is installed. Run: sudo dnf remove akmod-nvidia",
            "Blocked: headers for test are not installed. Run: sudo dnf install kernel-devel-test",
        ]
        state = {
            "gpu": "RTX 3080 Ti Laptop GPU", "family": "ampere", "notebook": True,
            "smi": "", "dynamic_boost": True, "secure_boot": False, "lockdown": "none",
            "sign": False, "distro": "fedora", "displays": [], "kernel": "test",
            "headers": "kernel-devel-test", "verdict": blockers[0], "refusals": blockers, "advice": [],
        }
        for json_mode in (False, True):
            output = io.StringIO()
            with (
                patch.object(preflight, "inspect", return_value=state),
                patch("sys.argv", ["preflight"] + (["--json"] if json_mode else [])),
                redirect_stdout(output),
            ):
                self.assertTrue(preflight.main())
            if json_mode:
                self.assertEqual(json.loads(output.getvalue()), state)
            else:
                self.assertIn(f"{'Verdict':14}{blockers[0]}\nAlso blocked: {blockers[1]}\n", output.getvalue())

    def test_packaged_driver_boundaries(self):
        fedora = ["nvidia-settings", "nvidia-persistenced", "nvidia-modprobe", "mesa", "akmod-nvidia"]
        self.assertEqual(preflight.conflicting("fedora", fedora), sorted(fedora[:-2] + [fedora[-1]]))
        arch = [
            "nvidia-utils",
            "lib32-nvidia-utils",
            "nvidia-settings",
            "nvidia-prime",
            "nvidia-open",
            "linux-cachyos-nvidia-open",
            "linux-cachyos-lts-nvidia",
        ]
        self.assertEqual(preflight.conflicting("cachyos", arch), sorted(arch[4:]))
        self.assertEqual(preflight.conflicting("cachyos", ["linux-firmware-nvidia", "linux-firmware"]), [])
        self.assertEqual(
            preflight.conflicting("ubuntu", ["libnvidia-gl-615", "nvidia-utils-615", "mesa"]),
            ["libnvidia-gl-615", "nvidia-utils-615"],
        )

    def test_secure_boot_and_lockdown_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            efi = root / "efi"
            lockdown = root / "lockdown"
            self.assertEqual(preflight.security(efi, lockdown), (False, None))
            (efi / "efivars").mkdir(parents=True)
            self.assertEqual(preflight.security(efi, lockdown), (None, None))
            (efi / "efivars/SecureBoot-test").write_bytes(bytes(4) + b"\x01")
            lockdown.write_text("[none] integrity confidentiality")
            self.assertEqual(preflight.security(efi, lockdown), (True, "none"))
            lockdown.write_text("none [integrity] confidentiality")
            self.assertEqual(preflight.security(efi, lockdown), (True, "integrity"))

    def test_ubuntu_package_query_excludes_removed_configuration_only_packages(self):
        with patch.object(preflight, "output", return_value="nvidia-old config-files\nlibnvidia-new installed\n"):
            self.assertEqual(preflight.packages("ubuntu"), ["libnvidia-new"])

    def test_common_paths_read_only_and_exact_unblock_command(self):
        for distro, version, name in [
            ("fedora", "43", "GA103M RTX 3080 Ti Laptop GPU"),
            ("ubuntu", "24.04", "AD104 RTX 4070"),
            ("cachyos", "", "AD106M RTX 4070 Laptop GPU"),
            ("arch", "", "GA102 RTX 3080"),
            ("nixos", "", "GA103M RTX 3080 Ti Laptop GPU"),
        ]:
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)

                def path(value):
                    return Path(value) if str(value).startswith(str(root)) else root / str(value).lstrip("/")

                gpu = path("/sys/bus/pci/devices/0000:01:00.0")
                gpu.mkdir(parents=True)
                (gpu / "vendor").write_text("0x10de")
                (gpu / "class").write_text("0x030000")
                before = sorted(str(p) for p in root.rglob("*"))
                with (
                    patch.object(preflight, "Path", side_effect=path),
                    patch.object(preflight, "release", return_value={"ID": distro, "VERSION_ID": version}),
                    patch.object(preflight.platform, "machine", return_value="x86_64"),
                    patch.object(preflight, "security", return_value=(False, "none")),
                    patch.object(preflight, "packages", return_value=[]),
                    patch.object(
                        preflight, "output", side_effect=lambda *a: name if a[0] == "lspci" else ""
                    ) as queries,
                ):
                    result = preflight.inspect("test")
                    self.assertFalse(any(call.args[0] == "nvidia-smi" for call in queries.call_args_list))
                    self.assertEqual(result["device"], gpu.name)
                    if distro != "arch":
                        self.assertEqual(result["refusals"], [])
                        if distro != "nixos":
                            self.assertIn("Headers for test will be installed.", result["advice"])
                    if distro in ("fedora", "ubuntu"):
                        package = "akmod-nvidia" if distro == "fedora" else "nvidia-driver-595-open"
                        with patch.object(preflight, "packages", return_value=[package]):
                            blocked = preflight.inspect("test")
                        self.assertIn(package, blocked["verdict"])
                        if distro == "fedora":
                            self.assertIn("sudo dnf remove --no-autoremove " + package, blocked["verdict"])
                        else:
                            self.assertIn("sudo apt-get purge " + package, blocked["verdict"])
                            self.assertIn(
                                "Do not run apt autoremove until the project driver is installed.", blocked["advice"]
                            )
                        self.assertIn(", then run the preflight again.", blocked["verdict"])
                        self.assertNotIn("reboot", blocked["verdict"])
                    if distro == "nixos":
                        self.assertNotIn("install/linux.sh", result["verdict"])
                        self.assertIn("docs/install.md#install-on-nixos", result["verdict"])
                        for sign in (False, True):
                            output = io.StringIO()
                            with (
                                patch.object(preflight, "inspect", return_value={**result, "sign": sign}),
                                patch("sys.argv", ["preflight"]),
                                redirect_stdout(output),
                            ):
                                self.assertFalse(preflight.main())
                            self.assertIn("signing is your boot chain's responsibility", output.getvalue())
                            self.assertIn("this module does not sign", output.getvalue())
                            self.assertNotIn("Machine Owner Key", output.getvalue())
                    if distro == "arch":
                        self.assertIn("CachyOS is the tested Arch derivative", result["verdict"])
                    elif distro == "cachyos":
                        self.assertIn("stock range only", result["verdict"])
                    elif distro == "ubuntu":
                        self.assertIn("power only", result["verdict"])
                    else:
                        self.assertTrue(result["verdict"].startswith("Supported."))
                    if distro == "nixos":
                        other = gpu.with_name("0000:02:00.0")
                        other.mkdir()
                        (other / "vendor").write_text("0x10de")
                        (other / "class").write_text("0x030000")
                        multiple = preflight.inspect("test")
                        self.assertIn(
                            "nix run github:Nickdom1/gpu-power-policy#preflight -- --device", multiple["verdict"]
                        )
                        self.assertNotIn("install/preflight.sh", multiple["verdict"])
                        for child in other.iterdir():
                            child.unlink()
                        other.rmdir()
                self.assertEqual(before, sorted(str(p) for p in root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
