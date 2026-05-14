from __future__ import annotations

import unittest

from relayinner_display.ipc import (
    IPCError,
    decode_message,
    encode_message,
    validate_daemon_message,
    validate_session_message,
)


class IPCTests(unittest.TestCase):
    def test_encode_decode_round_trip(self) -> None:
        payload = {
            "type": "connect_console",
            "backend": "moonlight",
            "launcher": "moonlight",
            "cwd": "/var/lib/relayinner-display/moonlight",
            "argv": [
                "moonlight",
                "stream",
                "192.168.50.20",
                "Desktop",
                "--resolution",
                "1920x1080",
                "--display-mode",
                "fullscreen",
            ],
        }
        decoded = decode_message(encode_message(payload))

        self.assertEqual(validate_daemon_message(decoded), payload)

    def test_legacy_connect_spice_is_still_accepted(self) -> None:
        payload = {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}

        self.assertEqual(validate_daemon_message(payload), payload)

    def test_display_power_messages_validate(self) -> None:
        daemon_payload = {"type": "display_power", "state": "off", "output": "HDMI-A-1"}
        session_payload = {"type": "console_started", "backend": "spice", "pid": 1234}

        self.assertEqual(validate_daemon_message(daemon_payload), daemon_payload)
        self.assertEqual(validate_session_message(session_payload), session_payload)

    def test_show_waiting_accepts_pairing_details(self) -> None:
        payload = {
            "type": "show_waiting",
            "reason": "pairing_required",
            "details": {
                "backend": "moonlight",
                "host": "192.168.50.20",
                "pin": "1234",
                "instructions": "Open Sunshine and enter this PIN.",
            },
        }

        self.assertEqual(validate_daemon_message(payload), payload)

    def test_console_lifecycle_messages_allow_legacy_missing_backend(self) -> None:
        payload = {"type": "console_exited", "code": 1, "signal": 0}

        self.assertEqual(validate_session_message(payload), payload)

    def test_unknown_message_type_is_rejected(self) -> None:
        payload = decode_message(b'{"type":"bogus"}\n')
        with self.assertRaises(IPCError):
            validate_daemon_message(payload)

    def test_decode_rejects_malformed_payloads(self) -> None:
        cases = [
            ("   ", "IPC message must not be empty"),
            ("not json", "Invalid JSON IPC payload"),
            ("[]", "IPC payload must be a JSON object"),
            ('{"type":""}', "IPC payload must contain a non-empty string 'type'"),
            ('{"type": 1}', "IPC payload must contain a non-empty string 'type'"),
        ]

        for raw_payload, message in cases:
            with self.subTest(raw_payload=raw_payload):
                with self.assertRaisesRegex(IPCError, message):
                    decode_message(raw_payload)

    def test_invalid_power_state_is_rejected(self) -> None:
        with self.assertRaises(IPCError):
            validate_daemon_message({"type": "display_power", "state": "sleep", "output": ""})

    def test_invalid_argv_is_rejected(self) -> None:
        with self.assertRaises(IPCError):
            validate_daemon_message(
                {
                    "type": "connect_console",
                    "backend": "spice",
                    "launcher": "remote-viewer",
                    "argv": ["", "--full-screen"],
                }
            )

    def test_relative_cwd_is_rejected(self) -> None:
        with self.assertRaises(IPCError):
            validate_daemon_message(
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": "moonlight",
                    "argv": [
                        "moonlight",
                        "stream",
                        "192.168.50.20",
                        "Desktop",
                        "--display-mode",
                        "fullscreen",
                    ],
                }
            )

    def test_invalid_waiting_details_are_rejected(self) -> None:
        with self.assertRaises(IPCError):
            validate_daemon_message(
                {
                    "type": "show_waiting",
                    "reason": "pairing_required",
                    "details": {"pin": ""},
                }
            )

    def test_unknown_extra_fields_are_rejected(self) -> None:
        payload = {"type": "session_ready", "unexpected": True}
        with self.assertRaises(IPCError):
            validate_session_message(payload)

    def test_validation_reports_missing_type_and_required_fields(self) -> None:
        cases = [
            (validate_session_message, {}, "session message is missing a valid 'type'"),
            (
                validate_daemon_message,
                {"type": "connect_spice"},
                "Missing required field 'vv_path' for connect_spice",
            ),
        ]

        for validator, payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(IPCError, message):
                    validator(payload)


if __name__ == "__main__":
    unittest.main()
