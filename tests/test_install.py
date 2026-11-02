import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "install/lib.sh"


class InstallerTests(unittest.TestCase):
    def shell(self, script, root):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; ' + script, "test", str(LIB)], cwd=root, capture_output=True, text=True
        )

    def test_mokutil_zero_exit_does_not_prove_enrollment(self):
        for message, status, enrolled in (
            ("MOK.der is not enrolled", 0, False),
            ("MOK.der is not enrolled", 1, False),
            ("MOK.der is already in the enrollment request", 0, False),
            ("Failed to read MokListRT", 1, False),
            ("MOK.der is already enrolled", 0, True),
            ("MOK.der is already enrolled", 1, True),
        ):
            with self.subTest(message=message):
                result = subprocess.run(
                    [
                        "bash", "-c",
                        'source "$1"; mokutil() { printf "%s\\n" "$MOK_RESULT"; return "$MOK_STATUS"; }; '
                        'mok_enrolled MOK.der',
                        "test", str(REPO / "install/sign.sh"),
                    ],
                    env={**os.environ, "MOK_RESULT": message, "MOK_STATUS": str(status)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, enrolled, result.stderr)

    def test_signing_prompt_precedes_driver_dependencies_and_source_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            shutil.copytree(REPO / "install", checkout / "install", copy_function=shutil.copyfile)
            for path in (checkout / "install").glob("*.sh"):
                source = path.read_text()
                source = re.sub(r"/(?:etc|lib|usr/local|var|run)/", lambda m: str(root) + m[0], source)
                source = source.replace('export PATH=/usr/sbin:/usr/bin:/sbin:/bin', ':')
                source = source.replace('[[ $EUID == 0 ]]', 'true')
                path.write_text(source)
            (checkout / "install/preflight.py").write_text(
                'print(\'{"device":"0000:01:00.0","sign":true,"pkgbase":""}\')\n'
            )
            for name, contents in {
                "etc/os-release": "ID=ubuntu\nVERSION_ID=24.04\n",
                "var/lib/shim-signed/mok/MOK.priv": "fake private key",
                "var/lib/shim-signed/mok/MOK.der": "fake certificate",
                "run/lock/placeholder": "",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            binaries = root / "bin"
            binaries.mkdir()
            for name, script in {
                "mokutil": 'echo "mokutil $*" >> "$CALLS"; [[ $1 != --test-key ]]',
                "openssl": ":", "update-secureboot-policy": ":", "modinfo": "exit 1", "flock": ":",
                "apt-get": 'echo "dependencies $*" >> "$CALLS"',
                "git": 'echo "source-fetch $*" >> "$CALLS"; exit 77',
            }.items():
                binary = binaries / name
                binary.write_text("#!" + shutil.which("bash") + "\n" + script + "\n")
                binary.chmod(0o755)
            result = subprocess.run(
                ["bash", str(checkout / "install/linux.sh")], cwd=root,
                env={**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"], "CALLS": str(root / "calls")},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 77, result.stderr)
            calls = (root / "calls").read_text().splitlines()
            timeout = calls.index("mokutil --timeout -1")
            prompt = next(i for i, call in enumerate(calls) if call.startswith("mokutil --import "))
            self.assertLess(timeout, prompt)
            dependencies = next(i for i, call in enumerate(calls) if call.startswith("dependencies"))
            fetch = next(i for i, call in enumerate(calls) if call.startswith("source-fetch"))
            self.assertLess(prompt, dependencies)
            self.assertLess(dependencies, fetch)
            self.assertIn("Enroll MOK, Continue, Yes", result.stdout)
            log = root / "var/log/gpu-power-policy/install.log"
            self.assertTrue(log.exists())
            self.assertIn("Installation failed. Log: " + str(log), result.stdout)
            self.assertIn("Fetching and patching", log.read_text())

    def test_raise_finds_installed_editor_and_shows_configure_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            shutil.copytree(REPO / "install", checkout / "install", copy_function=shutil.copyfile)
            for path in (checkout / "install").glob("*.sh"):
                source = re.sub(r"/(?:etc|lib|usr/local|var|run|proc)/", lambda m: str(root) + m[0], path.read_text())
                source = source.replace('export PATH=/usr/sbin:/usr/bin:/sbin:/bin', ':')
                path.write_text(source.replace('[[ $EUID == 0 ]]', 'true'))
            (checkout / "install/preflight.py").write_text(
                'print(\'{"device":"0000:01:00.0","sign":false,"pkgbase":""}\')\n'
            )
            (checkout / "src").mkdir()
            # Exercise the installer's real configure subprocess and output routing.
            (checkout / "src/gpu-power.py").write_text(
                'import shutil,subprocess,sys\n'
                'assert shutil.which("nvbios"), "installed editor was not found"\n'
                'subprocess.run(["nvbios"],check=True)\n'
                'message="current ceiling is outside the editable range"\n'
                'print(message,file=sys.stdout if "--json" in sys.argv else sys.stderr)\n'
                'sys.exit(1)\n'
            )
            for name, contents in {
                "etc/os-release": "ID=ubuntu\nVERSION_ID=24.04\n",
                "proc/driver/nvidia/gpus/0000:01:00.0/boot_vbios": "ROM",
                "usr/local/lib/gpu-power/bin/nvbios": "#!" + shutil.which("bash") + "\ntouch \"$EDITOR_CALLED\"\n",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", str(checkout / "install/linux.sh"), "--current-ceiling", "+8%"], cwd=root,
                env={**os.environ, "EDITOR_CALLED": str(root / "editor-called")}, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue((root / "editor-called").exists(), result.stderr)
            self.assertIn("current ceiling is outside the editable range", result.stderr)
            self.assertFalse((root / "var/log/gpu-power-policy/install.log").exists())

    def test_failed_kernel_hook_prints_repair_without_changing_boot_default(self):
        for distro in ("fedora", "ubuntu", "cachyos"):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                installer = root / "usr/local/lib/gpu-power/installer/install"
                shutil.copytree(REPO / "install", installer, copy_function=shutil.copyfile)
                for path in installer.glob("*.sh"):
                    source = re.sub(r"/(?:etc|usr/local|var|boot)/", lambda m: str(root) + m[0], path.read_text())
                    source = source.replace('export PATH=/usr/sbin:/usr/bin:/sbin:/bin', ':')
                    path.write_text(source)
                (installer / "rebuild-module.sh").write_text("exit 1\n")
                (root / "etc").mkdir(exist_ok=True)
                (root / "etc/os-release").write_text("ID=" + distro + "\n")
                (root / "boot").mkdir()
                kernel = subprocess.check_output(["uname", "-r"], text=True).strip()
                (root / "boot" / ("vmlinuz-" + kernel)).touch()
                (root / "default").write_text("original")
                binaries = root / "bin"
                binaries.mkdir()
                for name in ("grubby", "grub-set-default", "update-grub"):
                    path = binaries / name
                    path.write_text("#!" + shutil.which("bash") + '\nprintf changed > "$BOOT_DEFAULT"\n')
                    path.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(installer / "kernel-hook.sh"), "rebuild", "new-kernel"],
                    env={**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"],
                         "BOOT_DEFAULT": str(root / "default")}, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("--kernel new-kernel", result.stderr)
                self.assertEqual((root / "default").read_text(), "original")
                self.assertIn("did not build", (root / "var/lib/gpu-power-policy/kernel-notice").read_text())

    def test_kernel_rescan_retains_all_failures_and_skips_current_or_headerless(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kernel in ("a-bad", "b-good", "c-bad", "d-current", "e-no-headers"):
                path = root / "modules" / kernel
                path.mkdir(parents=True)
                if kernel != "e-no-headers":
                    (path / "build").mkdir()
            (root / "hook").write_text(
                f'source "{LIB}"\nclear_kernel_notice "$2" notice\n'
                'printf "%s\\n" "$2" >> calls\n'
                'if [[ $2 == *bad ]]; then echo "the module for $2 did not build; Run: repair" >> notice; exit 1; fi\n'
            )
            result = self.shell(
                'installed_for() { [[ $1 == d-current ]]; }; rebuild_installed_kernels ./hook ./modules', root
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "notice").read_text().splitlines(), [
                "the module for a-bad did not build; Run: repair", "the module for c-bad did not build; Run: repair",
            ])
            self.assertEqual((root / "calls").read_text().splitlines(), ["a-bad", "b-good", "c-bad"])
            (root / "hook").write_text(f'source "{LIB}"\nclear_kernel_notice "$2" notice\n')
            result = self.shell(
                'installed_for() { return 1; }; rebuild_installed_kernels ./hook ./modules', root
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "notice").exists())

    def test_successful_repair_clears_only_its_kernel_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notice = root / "notice"
            one = "gpu-power-policy: the module for 7.2.11 did not build; Run: repair 7.2.11"
            two = "gpu-power-policy: the module for 7.2.11-lts did not build; Run: repair 7.2.11-lts"
            notice.write_text(one + "\n" + two + "\n")
            for kernel, remaining in (("7.2.11", two + "\n"), ("7.2.11-lts", None), ("absent", None)):
                result = self.shell(f'clear_kernel_notice {kernel} notice', root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(notice.read_text() if notice.exists() else None, remaining)

    def test_cachyos_refresh_uses_native_limine_generator_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for available, expected in (("limine-mkinitcpio mkinitcpio dracut", "limine-mkinitcpio"),
                                        ("mkinitcpio dracut", "mkinitcpio -P"),
                                        ("dracut", "dracut --regenerate-all --force"), ("", None)):
                with self.subTest(available=available):
                    result = self.shell(
                        'source "' + str(REPO / "install/lifecycle.sh") + '"; ID=cachyos; '
                        'command() { [[ " ' + available + ' " == *" $2 "* ]]; }; '
                        'limine-mkinitcpio() { echo "limine-mkinitcpio"; }; '
                        'mkinitcpio() { echo "mkinitcpio $*"; }; dracut() { echo "dracut $*"; }; '
                        'refresh_initramfs', root,
                    )
                    if expected:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("No initramfs generator", result.stderr)

    def test_disable_keeps_module_inclusion_but_removes_the_image_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = ["etc/initramfs-tools/hooks/gpu-power-policy-modules",
                       "etc/mkinitcpio.conf.d/gpu-power-policy-modules.conf",
                       "etc/dracut.conf.d/gpu-power-policy-modules.conf"]
            for name in configs + ["etc/modprobe.d/gpu-power-policy.conf",
                                   "lib/firmware/nvidia/vbios-override/test.bin"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native module configuration")
            source = (REPO / "install/lifecycle.sh").read_text()
            source = re.sub(r"/(?:etc|lib|usr/local|var|run)/", lambda m: str(root) + m[0], source)
            (root / "lifecycle.sh").write_text(source)
            result = self.shell(
                'source ./lifecycle.sh; systemctl() { :; }; refresh_initramfs() { touch refreshed; }; disable_project',
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "refreshed").exists())
            self.assertFalse((root / "etc/modprobe.d/gpu-power-policy.conf").exists())
            self.assertFalse((root / "lib/firmware/nvidia/vbios-override").exists())
            for name in configs:
                self.assertEqual((root / name).read_text(), "native module configuration")

    def test_remove_cleans_owned_files_and_legacy_backups_in_fake_root(self):
        owned = [
            "etc/modprobe.d/gpu-power-policy.conf", "etc/modprobe.d/gpu-power-policy-nouveau.conf",
            "etc/dracut.conf.d/gpu-power-policy.conf", "etc/initramfs-tools/hooks/gpu-power-policy",
            "etc/initramfs-tools/hooks/gpu-power-policy-modules",
            "etc/mkinitcpio.conf.d/gpu-power-policy-modules.conf", "etc/dracut.conf.d/gpu-power-policy-modules.conf",
            "etc/mkinitcpio.conf.d/gpu-power-policy.conf", "etc/gpu-power-policy.json",
            "etc/gpu-power-policy-install.json", "etc/systemd/system/gpu-power-policy-init.service",
            "etc/systemd/system/nvidia-powerd.service.d/gpu-power-policy.conf",
            "etc/kernel/install.d/40-gpu-power-policy.install",
            "etc/kernel/postinst.d/gpu-power-policy",
            "etc/pacman.d/hooks/85-gpu-power-policy.hook", "etc/profile.d/gpu-power-policy.sh",
            "usr/local/bin/gpu-power",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in owned + ["etc/pacman.conf", "etc/modprobe.d/unrelated.conf"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
                path.with_name(path.name + ".~1~").write_text("old")
            firmware = [root / "lib/firmware/nvidia" / suffix for suffix in (
                "vbios-override/gpu.bin", "615.71.09/vbios-override/gpu.bin", "615.78.08/gsp.bin",
            )]
            for path in firmware:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"firmware")
            source = (REPO / "install/lifecycle.sh").read_text()
            source = re.sub(r"/(?:etc|lib|usr/local|var|run)/", lambda m: str(root) + m[0], source)
            (root / "lifecycle.sh").write_text(source)
            result = self.shell(
                'source ./lifecycle.sh; ID=fedora; version=example; '
                'systemctl() { :; }; depmod() { :; }; nvidia-installer() { :; }; '
                'refresh_initramfs() { :; }; remove_project', root
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in owned:
                path = root / name
                self.assertFalse(path.exists(), name)
                self.assertFalse(list(path.parent.glob(path.name + ".~*~")), name)
            self.assertFalse(firmware[0].exists())
            self.assertFalse(firmware[1].exists())
            self.assertTrue(firmware[2].exists())
            self.assertTrue((root / "etc/pacman.conf.~1~").exists())
            self.assertTrue((root / "etc/modprobe.d/unrelated.conf").exists())

    def test_saved_flags_preserve_raised_requests_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = {"powerCeilingWatts": 115, "currentCeilingMilliamps": 99000}
            config = {
                "device": "0000:01:00.0", "romBaseline": {"family": "ampere", "values": stock},
                "powerCeilingWatts": 125, "currentCeilingMilliamps": 107000,
                "powerdTargetWatts": 120, "beyondTested": True,
            }
            def flags(extra=""):
                (root / "saved.json").write_text(json.dumps(config))
                result = self.shell('saved_flags saved.json 0000:01:00.0 ' + extra, root)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.splitlines()
            self.assertEqual(flags('--boot-power 110'), [
                '--power-ceiling', '125', '--current-ceiling', '107000', '--boot-power', '120',
                '--beyond-tested', '--boot-power', '110',
            ])
            config['romBaseline']['family'] = 'ada'
            self.assertNotIn('--current-ceiling', flags())
            config.update(stock)
            config['powerdTargetWatts'] = None
            config['beyondTested'] = False
            self.assertEqual(flags(), [])
            config['device'] = '0000:02:00.0'
            config['powerdTargetWatts'] = 120
            self.assertEqual(flags(), [])

    def test_bad_download_and_bad_cache_are_removed_and_retry_succeeds(self):
        digest = hashlib.sha256(b"good").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = f"download_driver driver.run https://example.invalid/driver {digest}"
            bad = self.shell("curl() { printf bad > driver.run.tmp; }; " + command, root)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("Rerun the installer", bad.stderr)
            self.assertFalse((root / "driver.run").exists())
            self.assertFalse((root / "driver.run.tmp").exists())
            (root / "driver.run").write_bytes(b"bad cached download")
            self.assertNotEqual(self.shell(command, root).returncode, 0)
            self.assertFalse((root / "driver.run").exists())
            good = self.shell("curl() { printf good > driver.run.tmp; }; " + command, root)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual((root / "driver.run").read_bytes(), b"good")



if __name__ == "__main__":
    unittest.main()
