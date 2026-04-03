from __future__ import annotations

from pathlib import Path
import unittest

from relayinner_display.config import AppConfig, PolicyConfig, RuntimeConfig, TargetConfig
from relayinner_display.session import SessionSupervisor


class FakeProcess:
    def __init__(self, pid: int = 2222) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout: int) -> int:
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class SessionSupervisorTests(unittest.TestCase):
    def test_connect_spice_launches_remote_viewer(self) -> None:
        launches: list[tuple[list[str], dict[str, str]]] = []

        def fake_factory(command: list[str], env: dict[str, str], text: bool) -> FakeProcess:
            launches.append((command, env))
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        )

        self.assertEqual(events, [{"type": "console_started", "pid": 9001}])
        self.assertEqual(
            launches[0][0],
            ["remote-viewer", "--full-screen", "/run/relayinner-display/current.vv"],
        )

    def test_intentional_disconnect_suppresses_console_exit_event(self) -> None:
        process = FakeProcess(pid=100)

        def fake_factory(command: list[str], env: dict[str, str], text: bool) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        )
        supervisor.handle_daemon_message({"type": "show_waiting", "reason": "vm_stopped"})

        self.assertTrue(process.terminated)
        self.assertIsNone(supervisor.poll_console())

    def test_unexpected_exit_reports_console_exited(self) -> None:
        process = FakeProcess(pid=100)

        def fake_factory(command: list[str], env: dict[str, str], text: bool) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        )
        process.returncode = 1

        self.assertEqual(
            supervisor.poll_console(),
            {"type": "console_exited", "code": 1, "signal": 0},
        )


def build_config() -> AppConfig:
    run_dir = Path("/run/relayinner-display")
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
