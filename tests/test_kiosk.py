from __future__ import annotations

from io import StringIO
import runpy
import sys
from unittest.mock import patch
import unittest
import warnings

from relayinner_display.kiosk import main


class KioskTests(unittest.TestCase):
    def test_entrypoint_execs_session_with_sanitized_env(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with patch.dict(
            "os.environ",
            {
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin",
                "UNSAFE_VALUE": "should-not-pass",
            },
            clear=True,
        ):
            result = main(
                ["--config", "/tmp/relay.toml", "--session-binary", "/opt/bin/session"],
                execvpe=fake_exec,
            )

        self.assertEqual(result, 0)
        self.assertEqual(captured["program"], "/opt/bin/session")
        self.assertEqual(
            captured["argv"],
            ["/opt/bin/session", "--config", "/tmp/relay.toml"],
        )
        self.assertEqual(
            captured["env"],
            {
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin",
                "XDG_SESSION_TYPE": "wayland",
            },
        )

    def test_entrypoint_reports_exec_failure(self) -> None:
        def failing_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            raise OSError("not executable")

        stderr = StringIO()

        with patch("sys.stderr", stderr):
            result = main(
                ["--config", "/tmp/relay.toml", "--session-binary", "/opt/bin/session"],
                execvpe=failing_exec,
            )

        self.assertEqual(result, 127)
        self.assertIn(
            "relayinner-display-session-entrypoint: failed to exec /opt/bin/session: not executable",
            stderr.getvalue(),
        )

    def test_module_entrypoint_dispatches_to_main(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv

        with (
            patch("os.execvpe", side_effect=fake_exec),
            patch.object(sys, "argv", ["relayinner-display-session-entrypoint", "--session-binary", "/opt/bin/session"]),
            warnings.catch_warnings(),
            self.assertRaises(SystemExit) as raised,
        ):
            warnings.filterwarnings(
                "ignore",
                message="'relayinner_display.kiosk' found in sys.modules.*",
                category=RuntimeWarning,
            )
            runpy.run_module("relayinner_display.kiosk", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(captured["program"], "/opt/bin/session")
        self.assertEqual(captured["argv"], ["/opt/bin/session", "--config", "/etc/relayinner-display/config.toml"])

    def test_entrypoint_uses_absolute_installed_session_launcher_without_path(self) -> None:
        captured: dict[str, object] = {}

        def fake_exec(program: str, argv: list[str], env: dict[str, str]) -> None:
            captured["program"] = program
            captured["argv"] = argv
            captured["env"] = env

        with patch.dict(
            "os.environ",
            {
                "HOME": "/var/lib/relayinner-display",
            },
            clear=True,
        ):
            result = main(
                ["--config", "/tmp/relay.toml"],
                execvpe=fake_exec,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            captured["program"],
            "/usr/local/lib/relayinner-display/relayinner-display-session",
        )
        self.assertEqual(
            captured["argv"],
            [
                "/usr/local/lib/relayinner-display/relayinner-display-session",
                "--config",
                "/tmp/relay.toml",
            ],
        )
        self.assertEqual(
            captured["env"],
            {
                "HOME": "/var/lib/relayinner-display",
                "XDG_SESSION_TYPE": "wayland",
            },
        )


if __name__ == "__main__":
    unittest.main()
