from __future__ import annotations

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
    main,
)


def write_config(
    root: Path,
    *,
    backend: str,
    display_output_name: str = "",
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
            {"LIBSEAT_BACKEND": "seatd", "PATH": "/usr/bin"},
        )

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
            },
        )
        self.assertIn("workspace 1 output HDMI-A-1", sway_config)
        self.assertIn(
            f"exec /opt/relay/session-entrypoint --config {config_path}",
            sway_config,
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


if __name__ == "__main__":
    unittest.main()
