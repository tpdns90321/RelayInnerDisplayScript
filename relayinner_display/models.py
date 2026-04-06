from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
import json
import os
import tempfile


class SessionState(str, Enum):
    BOOTING = "booting"
    WAITING_FOR_SESSION = "waiting_for_session"
    WAITING_FOR_VM = "waiting_for_vm"
    REQUESTING_CONSOLE = "requesting_console"
    SHOWING_CONSOLE = "showing_console"
    RECONNECTING_CONSOLE = "reconnecting_console"
    DISPLAY_SLEEPING = "display_sleeping"
    DEGRADED = "degraded"

    def public_value(self) -> str:
        return self.value


@dataclass
class RuntimeState:
    vmid: int
    node_name: str
    console_backend: str = "spice"
    active_console_backend: str | None = None
    vm_power_state: str = "unknown"
    session_ready: bool = False
    display_power_intent: str = "on"
    display_power_applied: str = "on"
    session_state: SessionState = SessionState.BOOTING
    degraded_reason: str | None = None
    last_console_exit: dict[str, str | int] | None = None
    last_connect_attempt_at: str | None = None
    power_state_since: str | None = None
    last_power_button_at: str | None = None
    power_button_action_in_flight: bool = False
    last_power_button_action: str | None = None
    last_power_button_result: str | None = None
    last_error: str | None = None

    def mark_connect_attempt(self, when: datetime) -> None:
        self.last_connect_attempt_at = when.isoformat().replace("+00:00", "Z")

    def mark_power_state_since(self, when: datetime) -> None:
        self.power_state_since = when.isoformat().replace("+00:00", "Z")

    def mark_power_button_press(self, when: datetime, action: str, result: str) -> None:
        self.last_power_button_at = when.isoformat().replace("+00:00", "Z")
        self.last_power_button_action = action
        self.last_power_button_result = result
        self.power_button_action_in_flight = result == "submitted"

    def mark_console_exit(
        self,
        when: datetime,
        code: int,
        signal: int,
        backend: str | None = None,
    ) -> None:
        payload: dict[str, str | int] = {
            "at": when.isoformat().replace("+00:00", "Z"),
            "code": code,
            "signal": signal,
        }
        if backend is not None:
            payload["backend"] = backend
        self.last_console_exit = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "vmid": self.vmid,
            "node_name": self.node_name,
            "console_backend": self.console_backend,
            "active_console_backend": self.active_console_backend,
            "appliance_state": self.session_state.public_value(),
            "session_state": self.session_state.value,
            "vm_power_state": self.vm_power_state,
            "session_ready": self.session_ready,
            "display_power_intent": self.display_power_intent,
            "display_power_applied": self.display_power_applied,
            "degraded_reason": self.degraded_reason,
            "last_console_exit": self.last_console_exit,
            "last_connect_attempt_at": self.last_connect_attempt_at,
            "power_state_since": self.power_state_since,
            "last_power_button_at": self.last_power_button_at,
            "power_button_action_in_flight": self.power_button_action_in_flight,
            "last_power_button_action": self.last_power_button_action,
            "last_power_button_result": self.last_power_button_result,
            "last_error": self.last_error,
        }


def write_runtime_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)

    os.replace(temp_path, path)
