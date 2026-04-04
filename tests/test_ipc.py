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
        payload = {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        decoded = decode_message(encode_message(payload))

        self.assertEqual(validate_daemon_message(decoded), payload)

    def test_display_power_messages_validate(self) -> None:
        daemon_payload = {"type": "display_power", "state": "off", "output": "HDMI-A-1"}
        session_payload = {"type": "display_power_applied", "state": "off"}

        self.assertEqual(validate_daemon_message(daemon_payload), daemon_payload)
        self.assertEqual(validate_session_message(session_payload), session_payload)

    def test_unknown_message_type_is_rejected(self) -> None:
        payload = decode_message(b'{"type":"bogus"}\n')
        with self.assertRaises(IPCError):
            validate_daemon_message(payload)

    def test_invalid_power_state_is_rejected(self) -> None:
        with self.assertRaises(IPCError):
            validate_daemon_message({"type": "display_power", "state": "sleep", "output": ""})

    def test_unknown_extra_fields_are_rejected(self) -> None:
        payload = {"type": "session_ready", "unexpected": True}
        with self.assertRaises(IPCError):
            validate_session_message(payload)


if __name__ == "__main__":
    unittest.main()
