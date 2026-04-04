from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from relayinner_display.config import (
    AppConfig,
    DisplayConfig,
    InputConfig,
    PolicyConfig,
    RuntimeConfig,
    TargetConfig,
)
from relayinner_display.daemon import DisplayDaemon
from relayinner_display.input import PowerButtonError
from relayinner_display.models import SessionState
from relayinner_display.proxmox import ProxmoxCommandError


class FakeProxmoxClient:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.status_index = 0
        self.request_spice_calls: list[tuple[str, int]] = []
        self.write_calls: list[Path] = []
        self.start_calls: list[int] = []
        self.shutdown_calls: list[tuple[int, int]] = []

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

    def start_vm(self, vmid: int) -> None:
        self.start_calls.append(vmid)

    def shutdown_vm(self, vmid: int, timeout_s: int) -> None:
        self.shutdown_calls.append((vmid, timeout_s))


class FailingProxmoxClient(FakeProxmoxClient):
    def __init__(self) -> None:
        super().__init__(["running"])

    def get_vm_status(self, vmid: int) -> str:
        raise ProxmoxCommandError("qm failed: missing VM")


class ActionFailingProxmoxClient(FakeProxmoxClient):
    def __init__(self, statuses: list[str], action: str) -> None:
        super().__init__(statuses)
        self.action = action

    def start_vm(self, vmid: int) -> None:
        if self.action == "start":
            self.start_calls.append(vmid)
            raise ProxmoxCommandError("qm start failed: permission denied")
        super().start_vm(vmid)

    def shutdown_vm(self, vmid: int, timeout_s: int) -> None:
        if self.action == "shutdown":
            self.shutdown_calls.append((vmid, timeout_s))
            raise ProxmoxCommandError("qm shutdown failed: timeout")
        super().shutdown_vm(vmid, timeout_s)


class FakePowerButtonSource:
    def __init__(self, press_counts: list[int]) -> None:
        self.press_counts = press_counts
        self.index = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def poll_presses(self) -> int:
        if self.index < len(self.press_counts):
            value = self.press_counts[self.index]
            self.index += 1
            return value
        return 0

    def close(self) -> None:
        self.closed = True


class FakePolicyChecker:
    def validate(self) -> None:
        return None


class RejectingPolicyChecker:
    def validate(self) -> None:
        raise PowerButtonError("Host power-button handling is not disabled")


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
            self.assertEqual(
                ready_actions,
                [
                    {"type": "display_power", "state": "on", "output": ""},
                    {"type": "show_waiting", "reason": "vm_stopped"},
                ],
            )

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

    def test_paused_vm_keeps_console_and_display_on(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            proxmox = FakeProxmoxClient(["running", "paused"])
            daemon = DisplayDaemon(config=config, proxmox=proxmox)
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            daemon.tick(now=start_time)
            daemon.handle_session_message({"type": "console_started", "pid": 4321}, now=start_time)

            actions = daemon.tick(now=start_time + timedelta(milliseconds=100))

        self.assertEqual(actions, [])
        self.assertTrue(daemon.console_running)
        self.assertEqual(daemon.state.display_power_intent, "on")
        self.assertEqual(daemon.state.session_state, SessionState.SHOWING_CONSOLE)

    def test_initial_off_state_waits_before_sleeping(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(
                Path(temp_dir),
                dpms_off_delay_ms=5000,
                power_state_stabilize_ms=3000,
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            ready_actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            self.assertEqual(
                ready_actions,
                [
                    {"type": "display_power", "state": "on", "output": ""},
                    {"type": "show_waiting", "reason": "vm_stopped"},
                ],
            )

            self.assertEqual(daemon.tick(now=start_time), [])
            self.assertEqual(daemon.tick(now=start_time + timedelta(milliseconds=7999)), [])

            off_actions = daemon.tick(now=start_time + timedelta(milliseconds=8000))
            self.assertEqual(off_actions, [{"type": "display_power", "state": "off", "output": ""}])

            daemon.handle_session_message(
                {"type": "display_power_applied", "state": "off"},
                now=start_time + timedelta(milliseconds=8000),
            )

        self.assertEqual(daemon.state.display_power_intent, "off")
        self.assertEqual(daemon.state.display_power_applied, "off")
        self.assertEqual(daemon.state.session_state, SessionState.DISPLAY_SLEEPING)

    def test_running_vm_wakes_display_and_requests_console(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["running"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            daemon.state.display_power_intent = "off"
            daemon.state.display_power_applied = "off"
            daemon.state.vm_power_state = "stopped"
            daemon.state.session_state = SessionState.DISPLAY_SLEEPING

            actions = daemon.tick(now=start_time + timedelta(milliseconds=100))

        self.assertEqual(
            actions,
            [
                {"type": "display_power", "state": "on", "output": ""},
                {"type": "connect_spice", "vv_path": str(config.runtime.spice_vv_path)},
            ],
        )
        self.assertEqual(daemon.state.display_power_intent, "on")
        self.assertEqual(daemon.state.session_state, SessionState.CONNECTING_CONSOLE)

    def test_status_poll_failure_preserves_console_and_display(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FailingProxmoxClient())
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.session_ready = True
            daemon.console_running = True
            daemon.console_pid = 4242
            daemon.state.vm_power_state = "running"
            daemon.state.display_power_intent = "on"
            daemon.state.display_power_applied = "on"
            daemon.state.session_state = SessionState.SHOWING_CONSOLE

            actions = daemon.tick(now=start_time)

        self.assertEqual(actions, [])
        self.assertTrue(daemon.console_running)
        self.assertEqual(daemon.state.session_state, SessionState.SHOWING_CONSOLE)
        self.assertEqual(daemon.state.display_power_intent, "on")
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

    def test_session_ready_reapplies_existing_off_intent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.state.vm_power_state = "stopped"
            daemon.state.display_power_intent = "off"
            daemon.state.display_power_applied = "off"
            daemon.on_session_disconnected()

            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(
            actions,
            [
                {"type": "display_power", "state": "off", "output": ""},
                {"type": "show_waiting", "reason": "vm_stopped"},
            ],
        )
        self.assertEqual(daemon.state.session_state, SessionState.DISPLAY_SLEEPING)

    def test_display_power_applied_message_is_accepted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            daemon.state.display_power_intent = "off"
            daemon.state.vm_power_state = "stopped"

            actions = daemon.handle_session_message(
                {"type": "display_power_applied", "state": "off"}
            )

        self.assertEqual(actions, [])
        self.assertEqual(daemon.state.display_power_applied, "off")
        self.assertEqual(daemon.state.session_state, SessionState.DISPLAY_SLEEPING)

    def test_power_button_press_starts_stopped_vm_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True, debounce_ms=2000)
            proxmox = FakeProxmoxClient(
                ["stopped", "stopped", "stopped", "stopped", "stopped", "running"]
            )
            source = FakePowerButtonSource([1, 2, 0])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=source,
                host_policy_checker=FakePolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            daemon.tick(now=start_time)
            daemon.tick(now=start_time + timedelta(milliseconds=500))
            daemon.tick(now=start_time + timedelta(milliseconds=2500))

        self.assertEqual(proxmox.start_calls, [101])
        self.assertEqual(proxmox.shutdown_calls, [])
        self.assertEqual(daemon.state.last_power_button_action, "start")
        self.assertEqual(daemon.state.last_power_button_result, "completed")
        self.assertFalse(daemon.state.power_button_action_in_flight)
        self.assertTrue(source.opened)

    def test_power_button_press_requests_shutdown_for_running_vm(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True)
            proxmox = FakeProxmoxClient(["running", "running", "stopping", "shutdown"])
            source = FakePowerButtonSource([1, 0, 0])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=source,
                host_policy_checker=FakePolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            daemon.tick(now=start_time)
            daemon.tick(now=start_time + timedelta(seconds=5))
            daemon.tick(now=start_time + timedelta(seconds=10))

        self.assertEqual(proxmox.start_calls, [])
        self.assertEqual(proxmox.shutdown_calls, [(101, 90)])
        self.assertEqual(daemon.state.last_power_button_action, "shutdown")
        self.assertEqual(daemon.state.last_power_button_result, "completed")
        self.assertFalse(daemon.state.power_button_action_in_flight)

    def test_power_button_press_ignores_transitional_vm_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True)
            proxmox = FakeProxmoxClient(["starting"])
            source = FakePowerButtonSource([1])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=source,
                host_policy_checker=FakePolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            daemon.tick(now=start_time)

        self.assertEqual(proxmox.start_calls, [])
        self.assertEqual(proxmox.shutdown_calls, [])
        self.assertEqual(daemon.state.last_power_button_result, "ignored_non_actionable")
        self.assertFalse(daemon.state.power_button_action_in_flight)

    def test_power_button_action_failure_is_recorded_without_crash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True)
            proxmox = ActionFailingProxmoxClient(["running"], action="shutdown")
            source = FakePowerButtonSource([1])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=source,
                host_policy_checker=FakePolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            daemon.tick(now=start_time)

        self.assertEqual(daemon.state.last_power_button_action, "shutdown")
        self.assertEqual(daemon.state.last_power_button_result, "failed")
        self.assertEqual(daemon.state.last_error, "qm shutdown failed: timeout")
        self.assertFalse(daemon.state.power_button_action_in_flight)

    def test_failed_power_button_action_is_still_debounced(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True)
            proxmox = ActionFailingProxmoxClient(["running", "running"], action="shutdown")
            source = FakePowerButtonSource([1, 1])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=source,
                host_policy_checker=FakePolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            daemon.tick(now=start_time)
            daemon.tick(now=start_time + timedelta(milliseconds=1000))

        self.assertEqual(proxmox.shutdown_calls, [(101, 90)])
        self.assertEqual(daemon.state.last_power_button_action, "shutdown")
        self.assertEqual(daemon.state.last_power_button_result, "ignored_debounced")
        self.assertFalse(daemon.state.power_button_action_in_flight)

    def test_startup_validation_failure_enters_degraded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True)
            proxmox = FakeProxmoxClient(["stopped"])
            daemon = DisplayDaemon(
                config=config,
                proxmox=proxmox,
                power_button_source=FakePowerButtonSource([0]),
                host_policy_checker=RejectingPolicyChecker(),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(
            daemon.state.last_error,
            "Host power-button handling is not disabled",
        )


def build_config(
    root: Path,
    *,
    output_name: str = "",
    power_helper: str = "wlopm",
    dpms_off_delay_ms: int = 5000,
    power_state_stabilize_ms: int = 3000,
    forward_power_button: bool = False,
    debounce_ms: int = 2000,
) -> AppConfig:
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
        display=DisplayConfig(
            output_name=output_name,
            power_helper=power_helper,
        ),
        input=InputConfig(
            power_button_event=Path("/dev/input/by-path/platform-i8042-serio-0-event-power"),
            forward_power_button=forward_power_button,
            debounce_ms=debounce_ms,
        ),
        policy=PolicyConfig(
            poll_interval_ms=2000,
            reconnect_initial_ms=1000,
            reconnect_max_ms=15000,
            command_timeout_s=10,
            dpms_policy="vm-power",
            dpms_off_delay_ms=dpms_off_delay_ms,
            power_state_stabilize_ms=power_state_stabilize_ms,
            power_button_action_when_running="shutdown",
            power_button_action_when_stopped="start",
            shutdown_timeout_s=90,
        ),
    )


if __name__ == "__main__":
    unittest.main()
