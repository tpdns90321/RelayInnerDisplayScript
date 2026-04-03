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

    def test_unknown_message_type_is_rejected(self) -> None:
        payload = decode_message(b'{"type":"bogus"}\n')
        with self.assertRaises(IPCError):
            validate_daemon_message(payload)

    def test_unknown_extra_fields_are_rejected(self) -> None:
        payload = {"type": "session_ready", "unexpected": True}
        with self.assertRaises(IPCError):
            validate_session_message(payload)


if __name__ == "__main__":
    unittest.main()
