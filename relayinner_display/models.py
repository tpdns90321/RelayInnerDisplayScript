from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import json
import os
import tempfile


class SessionState(str, Enum):
    BOOTING = "booting"
    WAITING_FOR_SESSION = "waiting_for_session"
    WAITING_FOR_VM = "waiting_for_vm"
    REQUESTING_CONSOLE = "requesting_console"
    CONNECTING_CONSOLE = "connecting_console"
    SHOWING_CONSOLE = "showing_console"
    RECONNECTING_CONSOLE = "reconnecting_console"
    DEGRADED = "degraded"


@dataclass
class RuntimeState:
    vmid: int
    node_name: str
    vm_power_state: str = "unknown"
    session_state: SessionState = SessionState.BOOTING
    last_connect_attempt_at: str | None = None
    last_error: str | None = None

    def mark_connect_attempt(self, when: datetime) -> None:
        self.last_connect_attempt_at = when.isoformat().replace("+00:00", "Z")

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "vmid": self.vmid,
            "node_name": self.node_name,
            "vm_power_state": self.vm_power_state,
            "session_state": self.session_state.value,
            "last_connect_attempt_at": self.last_connect_attempt_at,
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
