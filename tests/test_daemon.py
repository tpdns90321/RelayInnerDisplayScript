from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from relayinner_display.config import AppConfig, PolicyConfig, RuntimeConfig, TargetConfig
from relayinner_display.daemon import DisplayDaemon
from relayinner_display.models import SessionState
from relayinner_display.proxmox import ProxmoxCommandError


class FakeProxmoxClient:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.status_index = 0
        self.request_spice_calls: list[tuple[str, int]] = []
        self.write_calls: list[Path] = []

    def resolve_node_name(self, configured_name: str) -> str:
        return "pve-01"

    def get_vm_status(self, vmid: int) -> str:
        if self.status_index < len(self.statuses):
            value = self.statuses[self.status_index]
            self.status_index += 1
            return value
        return self.statuses[-1]

    def request_spice_config(self, node_name: str, vmid: int) -> dict[str, str]:
        self.request_spice_calls.append((node_name, vmid))
        return {"host": "127.0.0.1", "port": "61000"}

    def write_vv_file(self, path: Path, spice_config: dict[str, str]) -> None:
        self.write_calls.append(path)
        path.write_text("[virt-viewer]\nhost=127.0.0.1\nport=61000\n", encoding="utf-8")


class FailingProxmoxClient(FakeProxmoxClient):
    def __init__(self) -> None:
        super().__init__(["running"])

    def get_vm_status(self, vmid: int) -> str:
        raise ProxmoxCommandError("qm failed: missing VM")


class DisplayDaemonTests(unittest.TestCase):
    def test_running_vm_connects_and_reconnects_after_viewer_exit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            proxmox = FakeProxmoxClient(["running", "running"])
            daemon = DisplayDaemon(config=config, proxmox=proxmox)
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            ready_actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            self.assertEqual(ready_actions, [{"type": "show_waiting", "reason": "vm_stopped"}])

            tick_actions = daemon.tick(now=start_time)
            self.assertEqual(
                tick_actions,
                [{"type": "connect_spice", "vv_path": str(config.runtime.spice_vv_path)}],
            )
            self.assertEqual(daemon.state.session_state, SessionState.CONNECTING_CONSOLE)
            self.assertTrue(config.runtime.spice_vv_path.exists())

            daemon.handle_session_message({"type": "console_started", "pid": 4321}, now=start_time)
            self.assertEqual(daemon.state.session_state, SessionState.SHOWING_CONSOLE)

            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "code": 1, "signal": 0},
                now=start_time,
            )
            self.assertEqual(exit_actions, [{"type": "show_waiting", "reason": "reconnecting"}])
            self.assertEqual(daemon.state.session_state, SessionState.RECONNECTING_CONSOLE)

            early_tick = daemon.tick(now=start_time + timedelta(milliseconds=500))
            self.assertEqual(early_tick, [])

            reconnect_tick = daemon.tick(now=start_time + timedelta(milliseconds=1000))
            self.assertEqual(
                reconnect_tick,
                [{"type": "connect_spice", "vv_path": str(config.runtime.spice_vv_path)}],
            )
            self.assertEqual(len(proxmox.request_spice_calls), 2)

    def test_control_failure_enters_degraded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FailingProxmoxClient())
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.last_error, "qm failed: missing VM")

    def test_control_failure_disconnects_existing_console(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FailingProxmoxClient())
            daemon.console_running = True
            daemon.console_pid = 4242
            daemon.state.session_state = SessionState.SHOWING_CONSOLE

            actions = daemon._enter_degraded("qm failed: missing VM")

        self.assertEqual(
            actions,
            [
                {"type": "disconnect_console", "reason": "control_error"},
                {"type": "show_waiting", "reason": "degraded"},
            ],
        )

    def test_display_power_applied_message_is_accepted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))

            actions = daemon.handle_session_message({"type": "display_power_applied", "state": "off"})

        self.assertEqual(actions, [])


def build_config(root: Path) -> AppConfig:
    run_dir = root / "run"
    return AppConfig(
        target=TargetConfig(
            vmid=101,
            node_name="auto",
            guest_os="windows",
            console_backend="spice",
        ),
        runtime=RuntimeConfig(
            run_dir=run_dir,
            control_socket=run_dir / "session.sock",
            spice_vv_path=run_dir / "current.vv",
            log_namespace="relayinner-display",
        ),
        policy=PolicyConfig(
            poll_interval_ms=2000,
            reconnect_initial_ms=1000,
            reconnect_max_ms=15000,
            command_timeout_s=10,
        ),
    )


if __name__ == "__main__":
    unittest.main()
