from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from relayinner_display.config import (
    AppConfig,
    DisplayConfig,
    InputConfig,
    PolicyConfig,
    RuntimeConfig,
    TargetConfig,
)
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
        self.assertTrue(supervisor.view_state.console_active)
        self.assertTrue(supervisor.view_state.cursor_hidden)
        self.assertEqual(supervisor.view_state.status_text, "Connecting")
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
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")
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

        event = supervisor.poll_console()

        self.assertEqual(event, {"type": "console_exited", "code": 1, "signal": 0})
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")

    def test_display_power_uses_configured_helper_and_reports_applied(self) -> None:
        commands: list[tuple[list[str], dict[str, str]]] = []

        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            commands.append((command, env))
            return subprocess.CompletedProcess(command, 0, "", "")

        supervisor = SessionSupervisor(
            config=build_config(power_helper="relay-wlopm"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": "HDMI-A-1"}
        )

        self.assertEqual(events, [{"type": "display_power_applied", "state": "off"}])
        self.assertEqual(commands[0][0], ["relay-wlopm", "--off", "HDMI-A-1"])
        self.assertEqual(supervisor.view_state.status_text, "Display sleeping")

    def test_display_power_failure_is_nonfatal(self) -> None:
        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("wlopm")

        supervisor = SessionSupervisor(
            config=build_config(),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": ""}
        )

        self.assertEqual(events, [])
        self.assertEqual(supervisor.view_state.display_power_state, "on")


def build_config(power_helper: str = "wlopm") -> AppConfig:
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
        display=DisplayConfig(
            output_name="HDMI-A-1",
            power_helper=power_helper,
        ),
        input=InputConfig(
            power_button_event=Path("/dev/input/by-path/platform-i8042-serio-0-event-power"),
            forward_power_button=False,
            debounce_ms=2000,
        ),
        policy=PolicyConfig(
            poll_interval_ms=2000,
            reconnect_initial_ms=1000,
            reconnect_max_ms=15000,
            command_timeout_s=10,
            dpms_policy="vm-power",
            dpms_off_delay_ms=5000,
            power_state_stabilize_ms=3000,
            power_button_action_when_running="shutdown",
            power_button_action_when_stopped="start",
            shutdown_timeout_s=90,
        ),
    )


if __name__ == "__main__":
    unittest.main()
