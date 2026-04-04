from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest

from relayinner_display.bootstrap import (
    BootstrapError,
    HostBootstrapInstaller,
    HostInstallPaths,
    REQUIRED_PACKAGES,
    REQUIRED_SERVICES,
    build_installed_daemon_command,
    build_installed_kiosk_command,
    build_installed_seatd_command,
    render_daemon_service,
    render_kiosk_service,
    render_logind_override,
    render_sample_config,
)
from relayinner_display.config import load_config
from relayinner_display.input import LogindPowerButtonPolicyChecker


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["systemctl", "list-unit-files", "display-manager.service"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class BootstrapTests(unittest.TestCase):
    def test_render_sample_config_matches_checked_in_example_and_is_valid(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(
            (repo_root / "config.example.toml").read_text(encoding="utf-8"),
            render_sample_config(),
        )

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(render_sample_config(), encoding="utf-8")
            config = load_config(config_path)

        self.assertTrue(config.input.forward_power_button)
        self.assertEqual(config.runtime.run_dir, Path("/run/relayinner-display"))

    def test_render_logind_override_matches_parser_expectation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            override_dir = root / "logind.conf.d"
            override_dir.mkdir()
            override_path = override_dir / "90-relay.conf"
            override_path.write_text(render_logind_override(), encoding="utf-8")

            checker = LogindPowerButtonPolicyChecker(main_configs=(), dropin_dirs=(override_dir,))
            checker.validate()

    def test_installed_commands_target_host_paths(self) -> None:
        paths = HostInstallPaths()
        self.assertEqual(
            build_installed_daemon_command(paths),
            [
                "/usr/bin/python3",
                "/usr/local/lib/relayinner-display/relayinner-displayd",
                "--config",
                "/etc/relayinner-display/config.toml",
            ],
        )
        self.assertEqual(
            build_installed_kiosk_command(paths),
            ["/usr/bin/cage", "--", "/usr/local/lib/relayinner-display/session-entrypoint"],
        )
        self.assertEqual(
            build_installed_seatd_command(),
            ["/usr/bin/seatd", "-g", "relayinner-display"],
        )

    def test_rendered_services_include_required_units_and_restart_policy(self) -> None:
        daemon_unit = render_daemon_service()
        kiosk_unit = render_kiosk_service()

        self.assertIn(
            "ExecStart=/usr/bin/python3 /usr/local/lib/relayinner-display/relayinner-displayd --config /etc/relayinner-display/config.toml",
            daemon_unit,
        )
        self.assertIn("Restart=always", daemon_unit)
        self.assertIn("Requires=relayinner-display-seatd.service relayinner-displayd.service", kiosk_unit)
        self.assertIn("TTYPath=/dev/tty1", kiosk_unit)
        self.assertIn("Environment=SEATD_SOCK=/run/seatd.sock", kiosk_unit)

    def test_validate_host_rejects_missing_proxmox_or_systemd(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        installer = HostBootstrapInstaller(
            repo_root=repo_root,
            pveversion_finder=lambda _: None,
            systemd_runtime_path=Path("/tmp/not-systemd"),
        )
        with self.assertRaises(BootstrapError):
            installer.validate_host()

        installer = HostBootstrapInstaller(
            repo_root=repo_root,
            pveversion_finder=lambda _: "/usr/bin/pveversion",
            systemd_runtime_path=Path("/tmp/not-systemd"),
        )
        with self.assertRaises(BootstrapError):
            installer.validate_host()

    def test_install_preserves_existing_config_and_enables_services(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        runner = FakeRunner()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "etc/relayinner-display/config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("existing = true\n", encoding="utf-8")

            installer = HostBootstrapInstaller(
                repo_root=repo_root,
                install_root=root,
                command_runner=runner,
                output=lambda _: None,
                pveversion_finder=lambda _: "/usr/bin/pveversion",
                systemd_runtime_path=root / "run/systemd/system",
            )
            result = installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=False,
            )

            self.assertTrue(result.config_preserved)
            self.assertFalse(result.config_created)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "existing = true\n")
            self.assertTrue((root / "usr/local/lib/relayinner-display/relayinner_display").is_dir())
            self.assertTrue((root / "usr/local/share/relayinner-display/proxmox-host-setup.md").is_file())
            self.assertTrue(
                any(command == ["systemctl", "enable", *REQUIRED_SERVICES] for command in runner.commands)
            )

    def test_install_replaces_config_with_backup_when_requested(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        runner = FakeRunner()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "etc/relayinner-display/config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("old = true\n", encoding="utf-8")

            installer = HostBootstrapInstaller(
                repo_root=repo_root,
                install_root=root,
                command_runner=runner,
                output=lambda _: None,
                pveversion_finder=lambda _: "/usr/bin/pveversion",
                systemd_runtime_path=root / "run/systemd/system",
            )
            result = installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=True,
            )

            self.assertTrue(result.config_created)
            self.assertIsNotNone(result.config_backup_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), render_sample_config())
            self.assertTrue(result.config_backup_path is not None and result.config_backup_path.exists())

    def test_manual_steps_keep_vmid_edit_reminder(self) -> None:
        installer = HostBootstrapInstaller(
            repo_root=Path(__file__).resolve().parents[1],
            install_root=Path("/tmp/stage"),
            command_runner=FakeRunner(),
            output=lambda _: None,
            pveversion_finder=lambda _: "/usr/bin/pveversion",
            systemd_runtime_path=Path("/tmp/systemd"),
        )

        steps = installer.build_manual_steps()

        self.assertTrue(any("vmid" in step for step in steps))
        self.assertTrue(any(REQUIRED_SERVICES[0] in step for step in steps))

    def test_required_packages_match_spec(self) -> None:
        self.assertEqual(
            REQUIRED_PACKAGES,
            ("python3", "python3-evdev", "cage", "seatd", "virt-viewer", "wlopm"),
        )


if __name__ == "__main__":
    unittest.main()
