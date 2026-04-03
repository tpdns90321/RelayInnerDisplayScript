from __future__ import annotations

from numbers import Integral
from typing import Any, Callable, Mapping
import json


class IPCError(ValueError):
    """Raised when an IPC payload is malformed or unsupported."""


Validator = Callable[[object], bool]


def _is_integer(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


DAEMON_TO_SESSION_FIELDS: dict[str, dict[str, Validator]] = {
    "show_waiting": {"reason": _is_non_empty_string},
    "connect_spice": {"vv_path": _is_non_empty_string},
    "disconnect_console": {"reason": _is_non_empty_string},
    "health_ping": {},
}

SESSION_TO_DAEMON_FIELDS: dict[str, dict[str, Validator]] = {
    "session_ready": {},
    "console_started": {"pid": _is_integer},
    "console_exited": {"code": _is_integer, "signal": _is_integer},
    "session_error": {"reason": _is_non_empty_string},
}


def encode_message(message: Mapping[str, object]) -> bytes:
    return (json.dumps(dict(message), separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def decode_message(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")

    text = line.strip()
    if not text:
        raise IPCError("IPC message must not be empty")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IPCError(f"Invalid JSON IPC payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise IPCError("IPC payload must be a JSON object")
    message_type = payload.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise IPCError("IPC payload must contain a non-empty string 'type'")

    return payload


def validate_daemon_message(message: Mapping[str, object]) -> dict[str, Any]:
    return _validate_message(message, DAEMON_TO_SESSION_FIELDS, "daemon")


def validate_session_message(message: Mapping[str, object]) -> dict[str, Any]:
    return _validate_message(message, SESSION_TO_DAEMON_FIELDS, "session")


def _validate_message(
    message: Mapping[str, object],
    schema: dict[str, dict[str, Validator]],
    source: str,
) -> dict[str, Any]:
    payload = dict(message)
    message_type = payload.get("type")
    if not isinstance(message_type, str):
        raise IPCError(f"{source} message is missing a valid 'type'")
    if message_type not in schema:
        raise IPCError(f"Unsupported {source} message type: {message_type}")

    allowed_fields = schema[message_type]
    expected_keys = {"type", *allowed_fields.keys()}
    unexpected_keys = sorted(set(payload) - expected_keys)
    if unexpected_keys:
        extras = ", ".join(unexpected_keys)
        raise IPCError(f"Unexpected field(s) for {message_type}: {extras}")

    for field_name, validator in allowed_fields.items():
        if field_name not in payload:
            raise IPCError(f"Missing required field {field_name!r} for {message_type}")
        if not validator(payload[field_name]):
            raise IPCError(f"Invalid value for field {field_name!r} in {message_type}")

    return payload
