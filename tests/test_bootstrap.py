from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
from typing import Callable
import unittest
from unittest.mock import patch

from relayinner_display.bootstrap import (
    BootstrapError,
    HostBootstrapInstaller,
    HostInstallPaths,
    REQUIRED_PACKAGES,
    REQUIRED_SERVICES,
    RUNTIME_STATE_DIR,
    SERVICE_USER,
    SYSTEMD_START_LIMIT_BURST,
    SYSTEMD_START_LIMIT_INTERVAL_SEC,
    build_installed_daemon_command,
    build_installed_kiosk_command,
    build_installed_seatd_command,
    detect_drm_device_groups,
    render_daemon_service,
    render_kiosk_service,
    render_seatd_service,
    render_logind_override,
    render_sample_config,
    resolve_host_binary,
)
from relayinner_display.config import load_config
from relayinner_display.input import LogindPowerButtonPolicyChecker


FIXED_INSTALL_TIME = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_UNIT_STATES = {
    "getty@tty1.service": {
        "existed": True,
        "enabled_before": True,
        "active_before": True,
        "masked_before": False,
    },
    "display-manager.service": {
        "existed": False,
        "enabled_before": False,
        "active_before": False,
        "masked_before": False,
    },
}


class FakeRunner:
    def __init__(self, unit_states: dict[str, dict[str, bool]] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.unit_states = unit_states or DEFAULT_UNIT_STATES

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:2] == ["systemctl", "list-unit-files"]:
            unit_name = command[2]
            unit_state = self.unit_states.get(unit_name, DEFAULT_UNIT_STATES["display-manager.service"])
            if not unit_state["existed"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            unit_file_state = "masked" if unit_state["masked_before"] else (
                "enabled" if unit_state["enabled_before"] else "disabled"
            )
            return subprocess.CompletedProcess(command, 0, f"{unit_name} {unit_file_state}\n", "")
        if command[:2] == ["systemctl", "is-enabled"]:
            unit_name = command[2]
            unit_state = self.unit_states.get(unit_name, DEFAULT_UNIT_STATES["display-manager.service"])
            if not unit_state["existed"]:
                return subprocess.CompletedProcess(command, 1, "", "")
            enabled_state = "masked" if unit_state["masked_before"] else (
                "enabled" if unit_state["enabled_before"] else "disabled"
            )
            return subprocess.CompletedProcess(command, 0, f"{enabled_state}\n", "")
        if command[:2] == ["systemctl", "is-active"]:
            unit_name = command[2]
            unit_state = self.unit_states.get(unit_name, DEFAULT_UNIT_STATES["display-manager.service"])
            if not unit_state["existed"] or not unit_state["active_before"]:
                return subprocess.CompletedProcess(command, 3, "inactive\n", "")
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class BootstrapTests(unittest.TestCase):
    def stage_path(self, root: Path, host_path: Path) -> Path:
        return root / host_path.relative_to("/")

    def read_install_state(self, root: Path) -> dict[str, object]:
        install_state_path = self.stage_path(root, HostInstallPaths().install_state_path)
        with install_state_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def write_install_state(self, root: Path, install_state: dict[str, object]) -> None:
        install_state_path = self.stage_path(root, HostInstallPaths().install_state_path)
        install_state_path.parent.mkdir(parents=True, exist_ok=True)
        install_state_path.write_text(json.dumps(install_state, indent=2) + "\n", encoding="utf-8")

    def make_installer(
        self,
        *,
        root: Path,
        runner: FakeRunner,
        output: Callable[[str], None] | None = None,
        service_user_exists: bool,
    ) -> HostBootstrapInstaller:
        repo_root = Path(__file__).resolve().parents[1]
        return HostBootstrapInstaller(
            repo_root=repo_root,
            install_root=root,
            command_runner=runner,
            output=output,
            pveversion_finder=lambda _: "/usr/bin/pveversion",
            systemd_runtime_path=root / "run/systemd/system",
            now_provider=lambda: FIXED_INSTALL_TIME,
            service_user_exists_checker=lambda _: service_user_exists,
            kiosk_supplementary_groups_detector=lambda: (),
        )

    def install_fixture(
        self,
        *,
        root: Path,
        service_user_exists: bool,
    ) -> None:
        installer = self.make_installer(
            root=root,
            runner=FakeRunner(),
            output=lambda _: None,
            service_user_exists=service_user_exists,
        )
        installer.install(
            skip_host_validation=True,
            skip_package_install=True,
            replace_config=False,
        )

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

    def test_detect_drm_device_groups_reads_unique_card_and_render_groups(self) -> None:
        with TemporaryDirectory() as temp_dir:
            device_dir = Path(temp_dir)
            (device_dir / "card0").write_text("", encoding="utf-8")
            (device_dir / "card1").write_text("", encoding="utf-8")
            (device_dir / "renderD128").write_text("", encoding="utf-8")

            gid_map = {
                "card0": 44,
                "card1": 44,
                "renderD128": 104,
            }
            name_map = {
                44: "video",
                104: "render",
            }
            original_stat = Path.stat

            def fake_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
                stat_result = original_stat(path, *args, **kwargs)
                target_gid = gid_map.get(path.name, stat_result.st_gid)
                return os.stat_result(
                    (
                        stat_result.st_mode,
                        stat_result.st_ino,
                        stat_result.st_dev,
                        stat_result.st_nlink,
                        stat_result.st_uid,
                        target_gid,
                        stat_result.st_size,
                        stat_result.st_atime,
                        stat_result.st_mtime,
                        stat_result.st_ctime,
                    )
                )

            with patch("pathlib.Path.stat", new=fake_stat), patch(
                "relayinner_display.bootstrap.grp.getgrgid",
                side_effect=lambda gid: type("grpentry", (), {"gr_name": name_map[gid]})(),
            ):
                self.assertEqual(detect_drm_device_groups(device_dir), ("video", "render"))

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
            [
                "/usr/bin/cage",
                "--",
                "/usr/local/lib/relayinner-display/session-entrypoint",
            ],
        )
        self.assertEqual(
            build_installed_seatd_command(),
            ["/usr/bin/seatd", "-g", "relayinner-display"],
        )

    def test_host_binary_resolution_prefers_installed_path(self) -> None:
        with patch(
            "relayinner_display.bootstrap.which",
            side_effect=lambda binary, path=None: {
                "seatd": "/usr/sbin/seatd",
                "cage": "/usr/local/bin/cage",
            }.get(binary),
        ):
            self.assertEqual(resolve_host_binary("seatd", "/usr/bin/seatd"), "/usr/sbin/seatd")
            self.assertEqual(
                build_installed_seatd_command(),
                ["/usr/sbin/seatd", "-g", "relayinner-display"],
            )
            self.assertEqual(
                build_installed_kiosk_command(HostInstallPaths()),
                ["/usr/local/bin/cage", "--", "/usr/local/lib/relayinner-display/session-entrypoint"],
            )

    def test_rendered_services_include_required_units_and_restart_policy(self) -> None:
        daemon_unit = render_daemon_service()
        kiosk_unit = render_kiosk_service(supplementary_groups=("video", "render"))
        seatd_unit = render_seatd_service()

        self.assertIn(
            "ExecStart=/usr/bin/python3 /usr/local/lib/relayinner-display/relayinner-displayd --config /etc/relayinner-display/config.toml",
            daemon_unit,
        )
        self.assertIn("Restart=always", daemon_unit)
        self.assertIn(f"StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}", daemon_unit)
        self.assertIn(f"StartLimitBurst={SYSTEMD_START_LIMIT_BURST}", daemon_unit)
        self.assertIn("Requires=relayinner-display-seatd.service relayinner-displayd.service", kiosk_unit)
        self.assertIn(
            "ExecStart=/usr/bin/cage -- /usr/local/lib/relayinner-display/session-entrypoint",
            kiosk_unit,
        )
        self.assertNotIn("seatd-launch", kiosk_unit)
        self.assertIn("SupplementaryGroups=video render", kiosk_unit)
        self.assertIn("Environment=LIBSEAT_BACKEND=seatd", kiosk_unit)
        self.assertIn("TTYPath=/dev/tty1", kiosk_unit)
        self.assertIn("StandardOutput=journal", kiosk_unit)
        self.assertIn("StandardError=journal", kiosk_unit)
        self.assertNotIn("Environment=SEATD_SOCK=/run/seatd.sock", kiosk_unit)
        self.assertIn(f"StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}", kiosk_unit)
        self.assertIn(f"StartLimitBurst={SYSTEMD_START_LIMIT_BURST}", seatd_unit)

    def test_install_writes_kiosk_unit_with_detected_drm_groups(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        runner = FakeRunner()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            installer = HostBootstrapInstaller(
                repo_root=repo_root,
                install_root=root,
                command_runner=runner,
                output=lambda _: None,
                pveversion_finder=lambda _: "/usr/bin/pveversion",
                systemd_runtime_path=root / "run/systemd/system",
                now_provider=lambda: FIXED_INSTALL_TIME,
                service_user_exists_checker=lambda _: True,
                kiosk_supplementary_groups_detector=lambda: ("video", "render"),
            )

            installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=False,
            )

            kiosk_unit_path = root / "etc/systemd/system/relayinner-display-kiosk.service"
            kiosk_unit = kiosk_unit_path.read_text(encoding="utf-8")
            self.assertIn("SupplementaryGroups=video render", kiosk_unit)

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
                now_provider=lambda: FIXED_INSTALL_TIME,
                service_user_exists_checker=lambda _: True,
            )
            result = installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=False,
            )
            install_state = self.read_install_state(root)

            self.assertEqual(result.config_action, "preserved")
            self.assertTrue(result.config_preserved)
            self.assertFalse(result.config_created)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "existing = true\n")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o750)
            self.assertTrue((root / "usr/local/lib/relayinner-display/relayinner_display").is_dir())
            self.assertTrue((root / "usr/local/share/relayinner-display/proxmox-host-setup.md").is_file())
            self.assertEqual(install_state["config_state"]["action"], "preserved")
            self.assertIsNone(install_state["config_state"]["backup_path"])
            self.assertFalse(install_state["service_user"]["created_by_installer"])
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
                now_provider=lambda: FIXED_INSTALL_TIME,
                service_user_exists_checker=lambda _: True,
            )
            result = installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=True,
            )
            install_state = self.read_install_state(root)

            self.assertEqual(result.config_action, "replaced")
            self.assertTrue(result.config_created)
            self.assertIsNotNone(result.config_backup_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), render_sample_config())
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o750)
            self.assertTrue(result.config_backup_path is not None and result.config_backup_path.exists())
            self.assertEqual(install_state["config_state"]["action"], "replaced")
            self.assertEqual(
                install_state["config_state"]["backup_path"],
                str(result.config_backup_path),
            )

    def test_install_writes_install_state_with_schema_and_unit_history(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        runner = FakeRunner()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            installer = HostBootstrapInstaller(
                repo_root=repo_root,
                install_root=root,
                command_runner=runner,
                output=lambda _: None,
                pveversion_finder=lambda _: "/usr/bin/pveversion",
                systemd_runtime_path=root / "run/systemd/system",
                now_provider=lambda: FIXED_INSTALL_TIME,
                service_user_exists_checker=lambda _: False,
            )

            result = installer.install(
                skip_host_validation=True,
                skip_package_install=True,
                replace_config=False,
            )
            install_state_path = root / "var/lib/relayinner-display/install-state.json"
            install_state = self.read_install_state(root)

            self.assertEqual(result.config_action, "created")
            self.assertTrue(install_state_path.is_file())
            self.assertEqual(install_state_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(install_state["schema_version"], 1)
            self.assertEqual(install_state["installed_at"], "2026-04-04T12:00:00Z")
            self.assertEqual(
                install_state["managed_paths"]["config_path"],
                "/etc/relayinner-display/config.toml",
            )
            self.assertEqual(
                install_state["managed_paths"]["systemd_units"],
                [
                    "/etc/systemd/system/relayinner-display-seatd.service",
                    "/etc/systemd/system/relayinner-display-kiosk.service",
                    "/etc/systemd/system/relayinner-displayd.service",
                ],
            )
            self.assertEqual(install_state["config_state"]["action"], "created")
            self.assertIsNone(install_state["config_state"]["backup_path"])
            self.assertEqual(install_state["service_user"]["name"], "relayinner-display")
            self.assertTrue(install_state["service_user"]["created_by_installer"])
            self.assertEqual(
                install_state["conflicting_units"]["getty@tty1.service"],
                {
                    "existed": True,
                    "enabled_before": True,
                    "active_before": True,
                    "masked_before": False,
                    "changed_by_installer": True,
                },
            )
            self.assertEqual(
                install_state["conflicting_units"]["display-manager.service"],
                {
                    "existed": False,
                    "enabled_before": False,
                    "active_before": False,
                    "masked_before": False,
                    "changed_by_installer": False,
                },
            )

    def test_uninstall_preserves_config_and_removes_runtime_when_install_state_present(self) -> None:
        paths = HostInstallPaths()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.install_fixture(root=root, service_user_exists=False)

            runner = FakeRunner()
            outputs: list[str] = []
            installer = self.make_installer(
                root=root,
                runner=runner,
                output=outputs.append,
                service_user_exists=True,
            )

            installer.uninstall()

            self.assertTrue(self.stage_path(root, paths.config_path).is_file())
            self.assertFalse(self.stage_path(root, paths.lib_dir).exists())
            self.assertFalse(self.stage_path(root, paths.share_dir).exists())
            self.assertFalse(self.stage_path(root, paths.logind_override_path).exists())
            self.assertFalse(self.stage_path(root, paths.install_state_path).exists())
            self.assertFalse(self.stage_path(root, paths.service_home).exists())
            self.assertFalse(self.stage_path(root, RUNTIME_STATE_DIR).exists())
            for unit_path in paths.systemd_unit_paths:
                self.assertFalse(self.stage_path(root, unit_path).exists())

            self.assertIn(["systemctl", "unmask", "getty@tty1.service"], runner.commands)
            self.assertIn(["systemctl", "enable", "getty@tty1.service"], runner.commands)
            self.assertIn(["systemctl", "start", "getty@tty1.service"], runner.commands)
            self.assertIn(["userdel", SERVICE_USER], runner.commands)
            self.assertTrue(any("Preserving operator config" in message for message in outputs))

    def test_uninstall_leaves_existing_service_user_and_home_intact(self) -> None:
        paths = HostInstallPaths()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.install_fixture(root=root, service_user_exists=True)

            runner = FakeRunner()
            installer = self.make_installer(
                root=root,
                runner=runner,
                output=lambda _: None,
                service_user_exists=True,
            )

            installer.uninstall()

            self.assertTrue(self.stage_path(root, paths.service_home).is_dir())
            self.assertFalse(self.stage_path(root, paths.install_state_path).exists())
            self.assertNotIn(["userdel", SERVICE_USER], runner.commands)

    def test_uninstall_purge_removes_config_backups_and_empty_config_dir(self) -> None:
        paths = HostInstallPaths()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.install_fixture(root=root, service_user_exists=True)

            config_dir = self.stage_path(root, paths.config_dir)
            (config_dir / "config.toml.bak.20260404120001").write_text("backup-1\n", encoding="utf-8")
            (config_dir / "config.toml.bak.20260404120002").write_text("backup-2\n", encoding="utf-8")

            installer = self.make_installer(
                root=root,
                runner=FakeRunner(),
                output=lambda _: None,
                service_user_exists=True,
            )

            installer.uninstall(purge_config=True)

            self.assertFalse(self.stage_path(root, paths.config_path).exists())
            self.assertFalse(config_dir.exists())

    def test_uninstall_best_effort_warns_when_install_state_is_missing(self) -> None:
        paths = HostInstallPaths()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.install_fixture(root=root, service_user_exists=True)
            self.stage_path(root, paths.install_state_path).unlink()

            outputs: list[str] = []
            runner = FakeRunner()
            installer = self.make_installer(
                root=root,
                runner=runner,
                output=outputs.append,
                service_user_exists=True,
            )

            installer.uninstall()

            self.assertTrue(any(message.startswith("WARNING: install-state missing") for message in outputs))
            self.assertTrue(self.stage_path(root, paths.config_path).is_file())
            self.assertTrue(self.stage_path(root, paths.service_home).is_dir())
            self.assertNotIn(["userdel", SERVICE_USER], runner.commands)
            self.assertIn(["systemctl", "enable", "getty@tty1.service"], runner.commands)
            self.assertIn(["systemctl", "start", "getty@tty1.service"], runner.commands)

    def test_uninstall_restores_getty_without_enabling_when_it_was_previously_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.install_fixture(root=root, service_user_exists=True)
            install_state = self.read_install_state(root)
            install_state["conflicting_units"]["getty@tty1.service"] = {
                "existed": True,
                "enabled_before": False,
                "active_before": False,
                "masked_before": False,
                "changed_by_installer": True,
            }
            self.write_install_state(root, install_state)

            runner = FakeRunner()
            installer = self.make_installer(
                root=root,
                runner=runner,
                output=lambda _: None,
                service_user_exists=True,
            )

            installer.uninstall()

            self.assertIn(["systemctl", "unmask", "getty@tty1.service"], runner.commands)
            self.assertIn(["systemctl", "start", "getty@tty1.service"], runner.commands)
            self.assertNotIn(["systemctl", "enable", "getty@tty1.service"], runner.commands)

    def test_uninstall_restores_display_manager_only_when_install_state_proves_it_changed(self) -> None:
        for changed_by_installer in (False, True):
            with self.subTest(changed_by_installer=changed_by_installer):
                with TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    self.install_fixture(root=root, service_user_exists=True)
                    install_state = self.read_install_state(root)
                    install_state["conflicting_units"]["display-manager.service"] = {
                        "existed": True,
                        "enabled_before": True,
                        "active_before": True,
                        "masked_before": False,
                        "changed_by_installer": changed_by_installer,
                    }
                    self.write_install_state(root, install_state)

                    runner = FakeRunner()
                    installer = self.make_installer(
                        root=root,
                        runner=runner,
                        output=lambda _: None,
                        service_user_exists=True,
                    )

                    installer.uninstall()

                    display_manager_commands = [
                        ["systemctl", "unmask", "display-manager.service"],
                        ["systemctl", "enable", "display-manager.service"],
                        ["systemctl", "start", "display-manager.service"],
                    ]
                    if changed_by_installer:
                        for command in display_manager_commands:
                            self.assertIn(command, runner.commands)
                    else:
                        for command in display_manager_commands:
                            self.assertNotIn(command, runner.commands)

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
