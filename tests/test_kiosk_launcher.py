from __future__ import annotations

from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from relayinner_display.kiosk_launcher import (
    build_cage_command,
    build_sway_command,
    detect_connected_drm_outputs,
    main,
)


def write_config(
    root: Path,
    *,
    backend: str,
    display_output_name: str = "",
    display_drm_compatibility: str | None = None,
    kiosk_compositor: str | None = None,
) -> Path:
    config = textwrap.dedent(
        f"""\
        [target]
        vmid = 101
        node_name = "pve"
        guest_os = "windows"
        console_backend = "{backend}"

        [runtime]
        run_dir = "/run/relayinner-display"
        control_socket = "/run/relayinner-display/session.sock"
        log_namespace = "relayinner-display"

        [console]
        artifact_dir = "/run/relayinner-display/console"
        """
    )
    if kiosk_compositor is not None:
        config += textwrap.dedent(
            f"""\

            [kiosk]
            compositor = "{kiosk_compositor}"
            """
        )
    if backend == "moonlight":
        config += textwrap.dedent(
            """

            [console.moonlight]
            host = "192.168.50.20"
            """
        )
    else:
        config += textwrap.dedent(
            """

            [console.spice]
            vv_path = "/run/relayinner-display/console/spice-current.vv"
            """
        )
    config += textwrap.dedent(
        f"""

        [policy]
        poll_interval_ms = 2000
        reconnect_initial_ms = 1000
        reconnect_max_ms = 15000
        command_timeout_s = 10

        [display]
        output_name = "{display_output_name}"
        """
    )
    if display_drm_compatibility is not None:
        config += f'drm_compatibility = "{display_drm_compatibility}"\n'
    path = root / "config.toml"
    path.write_text(config, encoding="utf-8")
    return path


class KioskLauncherTests(unittest.TestCase):
    def test_build_compositor_commands_match_spec(self) -> None:
        self.assertEqual(
            build_cage_command(),
            ["cage", "--", "/usr/local/lib/relayinner-display/session-entrypoint"],
        )
        self.assertEqual(
            build_sway_command(),
            ["sway", "--config", "/run/relayinner-display/sway.config"],
        )

    def test_main_execs_cage_for_non_moonlight_auto(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            config_path = write_config(Path(temp_dir), backend="spice")
            with patch.dict(
                "os.environ",
                {"LIBSEAT_BACKEND": "seatd", "PATH": "/usr/bin"},
                clear=True,
            ):
                result = main(["--config", str(config_path)], execvpe=fake_exec)

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "cage")
        self.assertEqual(
            captured["argv"],
            ["cage", "--", "/usr/local/lib/relayinner-display/session-entrypoint"],
        )
        self.assertEqual(
            captured["env"],
            {
                "LIBSEAT_BACKEND": "seatd",
                "PATH": "/usr/bin",
                "WLR_DRM_NO_MODIFIERS": "1",
            },
        )

    def test_detect_connected_drm_outputs_reads_connected_connectors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connected = root / "card0-HDMI-A-1"
            connected.mkdir()
            (connected / "status").write_text("connected\n", encoding="utf-8")
            disconnected = root / "card0-DP-1"
            disconnected.mkdir()
            (disconnected / "status").write_text("disconnected\n", encoding="utf-8")

            outputs = detect_connected_drm_outputs(root)

        self.assertEqual(outputs, {"HDMI-A-1"})

    def test_detect_connected_drm_outputs_handles_missing_and_unreadable_status_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertIsNone(detect_connected_drm_outputs(root))

            connected = root / "card0-HDMI-A-1"
            connected.mkdir()
            (connected / "status").write_text("connected\n", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=OSError("denied")):
                outputs = detect_connected_drm_outputs(root)

        self.assertEqual(outputs, set())

    def test_main_writes_sway_config_and_execs_sway_for_moonlight_auto(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_config(
                root,
                backend="moonlight",
                display_output_name="HDMI-A-1",
            )
            sway_config_path = root / "run" / "sway.config"
            with patch.dict(
                "os.environ",
                {
                    "HOME": "/var/lib/relayinner-display",
                    "LIBSEAT_BACKEND": "seatd",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ):
                stdout = StringIO()
                with patch(
                    "relayinner_display.kiosk_launcher.detect_connected_drm_outputs",
                    return_value={"HDMI-A-1"},
                ), redirect_stdout(stdout):
                    result = main(
                        [
                            "--config",
                            str(config_path),
                            "--entrypoint-path",
                            "/opt/relay/session-entrypoint",
                            "--sway-config-path",
                            str(sway_config_path),
                        ],
                        execvpe=fake_exec,
                    )

            sway_config = sway_config_path.read_text(encoding="utf-8")
            sway_config_mode = sway_config_path.stat().st_mode & 0o777

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "sway")
        self.assertEqual(
            captured["argv"],
            ["sway", "--config", str(sway_config_path)],
        )
        self.assertEqual(
            captured["env"],
            {
                "HOME": "/var/lib/relayinner-display",
                "LIBSEAT_BACKEND": "seatd",
                "PATH": "/usr/bin",
                "WLR_DRM_NO_MODIFIERS": "1",
            },
        )
        self.assertEqual(sway_config_mode, 0o600)
        self.assertIn("workspace 1 output HDMI-A-1", sway_config)
        self.assertIn(
            f"exec /opt/relay/session-entrypoint --config {config_path}",
            sway_config,
        )
        self.assertNotIn("bar", sway_config)
        self.assertNotIn("bindsym", sway_config)
        self.assertNotIn("include", sway_config)
        self.assertIn("display_drm_compatibility=auto", stdout.getvalue())
        self.assertIn("effective_wlroots_drm_env=WLR_DRM_NO_MODIFIERS=1", stdout.getvalue())
        self.assertIn("requested output pin for workspace 1 on HDMI-A-1", stdout.getvalue())

    def test_main_omits_workspace_pin_when_output_name_is_empty(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_config(root, backend="moonlight")
            sway_config_path = root / "run" / "sway.config"
            result = main(
                [
                    "--config",
                    str(config_path),
                    "--sway-config-path",
                    str(sway_config_path),
                ],
                execvpe=fake_exec,
            )
            sway_config = sway_config_path.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(captured["argv"], ["sway", "--config", str(sway_config_path)])
        self.assertEqual(captured["env"]["WLR_DRM_NO_MODIFIERS"], "1")
        self.assertNotIn("workspace 1 output", sway_config)

    def test_main_warns_and_skips_workspace_pin_for_unavailable_output(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_config(
                root,
                backend="moonlight",
                display_output_name="HDMI-A-1",
            )
            sway_config_path = root / "run" / "sway.config"
            stdout = StringIO()
            with patch(
                "relayinner_display.kiosk_launcher.detect_connected_drm_outputs",
                return_value={"DP-1"},
            ), redirect_stdout(stdout):
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "--sway-config-path",
                        str(sway_config_path),
                    ],
                    execvpe=fake_exec,
                )
            sway_config = sway_config_path.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "sway")
        self.assertEqual(captured["env"]["WLR_DRM_NO_MODIFIERS"], "1")
        self.assertNotIn("workspace 1 output HDMI-A-1", sway_config)
        self.assertIn("requested output pin for workspace 1 on HDMI-A-1", stdout.getvalue())
        self.assertIn("WARNING: requested output pin for HDMI-A-1 is unavailable", stdout.getvalue())

    def test_main_disables_wlroots_drm_workarounds_when_configured_off(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            config_path = write_config(
                Path(temp_dir),
                backend="spice",
                display_drm_compatibility="off",
            )
            with patch.dict(
                "os.environ",
                {
                    "LIBSEAT_BACKEND": "seatd",
                    "PATH": "/usr/bin",
                    "WLR_DRM_NO_ATOMIC": "1",
                    "WLR_DRM_NO_MODIFIERS": "1",
                },
                clear=True,
            ):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = main(["--config", str(config_path)], execvpe=fake_exec)

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "cage")
        self.assertEqual(
            captured["env"],
            {"LIBSEAT_BACKEND": "seatd", "PATH": "/usr/bin"},
        )
        self.assertIn("display_drm_compatibility=off", stdout.getvalue())
        self.assertIn("effective_wlroots_drm_env=none", stdout.getvalue())

    def test_main_enables_legacy_drm_fallback_when_configured(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with TemporaryDirectory() as temp_dir:
            config_path = write_config(
                Path(temp_dir),
                backend="spice",
                display_drm_compatibility="legacy-drm",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = main(["--config", str(config_path)], execvpe=fake_exec)

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "cage")
        self.assertEqual(
            captured["env"]["WLR_DRM_NO_ATOMIC"],
            "1",
        )
        self.assertEqual(
            captured["env"]["WLR_DRM_NO_MODIFIERS"],
            "1",
        )
        self.assertIn("display_drm_compatibility=legacy-drm", stdout.getvalue())
        self.assertIn(
            "effective_wlroots_drm_env=WLR_DRM_NO_ATOMIC=1,WLR_DRM_NO_MODIFIERS=1",
            stdout.getvalue(),
        )

    def test_main_rejects_invalid_config_combination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = write_config(
                Path(temp_dir),
                backend="spice",
                kiosk_compositor="sway",
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(["--config", str(config_path)])

        self.assertEqual(result, 78)
        self.assertIn("console_backend='spice'", stderr.getvalue())
        self.assertIn("kiosk.compositor='sway'", stderr.getvalue())

    def test_main_reports_exec_failure(self) -> None:
        def failing_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            raise OSError("not executable")

        with TemporaryDirectory() as temp_dir:
            config_path = write_config(Path(temp_dir), backend="spice")
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(["--config", str(config_path)], execvpe=failing_exec)

        self.assertEqual(result, 127)
        self.assertIn(
            "relayinner-display-kiosk: failed to exec cage: not executable",
            stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
