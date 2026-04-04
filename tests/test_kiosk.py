from __future__ import annotations

from unittest.mock import patch
import unittest

from relayinner_display.kiosk import build_kiosk_service_command, main


class KioskTests(unittest.TestCase):
    def test_build_kiosk_service_command_matches_spec(self) -> None:
        self.assertEqual(
            build_kiosk_service_command(),
            [
                "seatd-launch",
                "--",
                "cage",
                "--",
                "/usr/local/lib/relayinner-display/session-entrypoint",
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
