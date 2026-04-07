from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from relayinner_display.config import (
    AppConfig,
    ConsoleConfig,
    ConsoleLookingGlassConfig,
    ConsoleMoonlightConfig,
    ConsoleSpiceConfig,
    ConsoleVncConfig,
    DisplayConfig,
    InputConfig,
    PolicyConfig,
    RuntimeConfig,
    TargetConfig,
)
from relayinner_display.daemon import DisplayDaemon
from relayinner_display.input import PowerButtonError
from relayinner_display.models import MoonlightPairState, SessionState
from relayinner_display.proxmox import (
    ProxmoxCommandError,
    VncConfigurationError,
    VncEndpoint,
    VncEndpointUnavailableError,
)


class FakeProxmoxClient:
    def __init__(
        self,
        statuses: list[str],
        *,
        vnc_validation_error: Exception | None = None,
        vnc_probe_error: Exception | None = None,
        vnc_endpoint: VncEndpoint | None = None,
    ) -> None:
        self.statuses = statuses
        self.status_index = 0
        self.request_spice_calls: list[tuple[str, int]] = []
        self.write_calls: list[Path] = []
        self.start_calls: list[int] = []
        self.shutdown_calls: list[tuple[int, int]] = []
        self.validate_vnc_calls: list[tuple[int, str, int]] = []
        self.probe_vnc_calls: list[tuple[str, int, float]] = []
        self.vnc_validation_error = vnc_validation_error
        self.vnc_probe_error = vnc_probe_error
        self.vnc_endpoint = vnc_endpoint

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

    def validate_vnc_configuration(
        self,
        vmid: int,
        *,
        bind_host: str,
        display_number: int,
    ) -> VncEndpoint:
        self.validate_vnc_calls.append((vmid, bind_host, display_number))
        if self.vnc_validation_error is not None:
            raise self.vnc_validation_error
        return self.vnc_endpoint or VncEndpoint(bind_host=bind_host, display_number=display_number)

    def probe_vnc_endpoint(self, bind_host: str, port: int, timeout_s: float = 1.0) -> None:
        self.probe_vnc_calls.append((bind_host, port, timeout_s))
        if self.vnc_probe_error is not None:
            raise self.vnc_probe_error


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


def moonlight_app_list_csv(*apps: str) -> str:
    rows = ["Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL\n"]
    for index, app in enumerate(apps, start=1):
        rows.append(f'"{app}",{index},false,false,false,false,""\n')
    return "".join(rows)


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
                    {"type": "show_waiting", "reason": "connecting"},
                ],
            )
            self.assertEqual(daemon.state.session_state, SessionState.REQUESTING_CONSOLE)

            tick_actions = daemon.tick(now=start_time)
            self.assertEqual(
                tick_actions,
                [
                    {
                        "type": "connect_console",
                        "backend": "spice",
                        "launcher": "remote-viewer",
                        "argv": [
                            "remote-viewer",
                            "--full-screen",
                            str(config.console.spice.vv_path),
                        ],
                    }
                ],
            )
            self.assertEqual(daemon.state.session_state, SessionState.REQUESTING_CONSOLE)
            self.assertTrue(config.console.spice.vv_path.exists())

            daemon.handle_session_message(
                {"type": "console_started", "backend": "spice", "pid": 4321},
                now=start_time,
            )
            self.assertEqual(daemon.state.session_state, SessionState.SHOWING_CONSOLE)

            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "backend": "spice", "code": 1, "signal": 0},
                now=start_time,
            )
            self.assertEqual(exit_actions, [{"type": "show_waiting", "reason": "reconnecting"}])
            self.assertEqual(daemon.state.session_state, SessionState.RECONNECTING_CONSOLE)

            early_tick = daemon.tick(now=start_time + timedelta(milliseconds=500))
            self.assertEqual(early_tick, [])

            reconnect_tick = daemon.tick(now=start_time + timedelta(milliseconds=1000))
            self.assertEqual(
                reconnect_tick,
                [
                    {
                        "type": "connect_console",
                        "backend": "spice",
                        "launcher": "remote-viewer",
                        "argv": [
                            "remote-viewer",
                            "--full-screen",
                            str(config.console.spice.vv_path),
                        ],
                    }
                ],
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
            daemon.handle_session_message(
                {"type": "console_started", "backend": "spice", "pid": 4321},
                now=start_time,
            )

            actions = daemon.tick(now=start_time + timedelta(milliseconds=100))

        self.assertEqual(actions, [])
        self.assertTrue(daemon.console_running)
        self.assertEqual(daemon.state.display_power_intent, "on")
        self.assertEqual(daemon.state.session_state, SessionState.SHOWING_CONSOLE)

    def test_running_vm_connects_and_reconnects_after_vnc_viewer_exit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="vnc")
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
                    {"type": "show_waiting", "reason": "connecting"},
                ],
            )

            tick_actions = daemon.tick(now=start_time)
            self.assertEqual(
                tick_actions,
                [
                    {
                        "type": "connect_console",
                        "backend": "vnc",
                        "launcher": "remote-viewer",
                        "argv": [
                            "remote-viewer",
                            "--full-screen",
                            "vnc://127.0.0.1:5977",
                        ],
                    }
                ],
            )
            self.assertEqual(proxmox.validate_vnc_calls, [(101, "127.0.0.1", 77)])
            self.assertEqual(proxmox.probe_vnc_calls, [("127.0.0.1", 5977, 1.0)])

            daemon.handle_session_message(
                {"type": "console_started", "backend": "vnc", "pid": 4321},
                now=start_time,
            )
            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "backend": "vnc", "code": 1, "signal": 0},
                now=start_time,
            )

            self.assertEqual(exit_actions, [{"type": "show_waiting", "reason": "reconnecting"}])
            reconnect_tick = daemon.tick(now=start_time + timedelta(milliseconds=1000))
            self.assertEqual(
                reconnect_tick,
                [
                    {
                        "type": "connect_console",
                        "backend": "vnc",
                        "launcher": "remote-viewer",
                        "argv": [
                            "remote-viewer",
                            "--full-screen",
                            "vnc://127.0.0.1:5977",
                        ],
                    }
                ],
            )

    def test_running_vm_connects_and_reconnects_after_looking_glass_exit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            shm_file = Path(temp_dir) / "kvmfr0"
            shm_file.write_text("ready", encoding="utf-8")
            shm_file.chmod(0o644)
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_shm_file=shm_file,
            )
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
                    {"type": "show_waiting", "reason": "connecting"},
                ],
            )

            tick_actions = daemon.tick(now=start_time)
            self.assertEqual(
                tick_actions,
                [
                    {
                        "type": "connect_console",
                        "backend": "looking-glass",
                        "launcher": "looking-glass-client",
                        "argv": [
                            "looking-glass-client",
                            "-F",
                            "-S",
                            "-g",
                            "auto",
                            "-f",
                            str(shm_file),
                        ],
                    }
                ],
            )

            daemon.handle_session_message(
                {"type": "console_started", "backend": "looking-glass", "pid": 4321},
                now=start_time,
            )
            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "backend": "looking-glass", "code": 1, "signal": 0},
                now=start_time,
            )

            self.assertEqual(exit_actions, [{"type": "show_waiting", "reason": "reconnecting"}])
            reconnect_tick = daemon.tick(now=start_time + timedelta(milliseconds=1000))
            self.assertEqual(
                reconnect_tick,
                [
                    {
                        "type": "connect_console",
                        "backend": "looking-glass",
                        "launcher": "looking-glass-client",
                        "argv": [
                            "looking-glass-client",
                            "-F",
                            "-S",
                            "-g",
                            "auto",
                            "-f",
                            str(shm_file),
                        ],
                    }
                ],
            )

    def test_looking_glass_connect_message_honors_configured_flags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            shm_file = Path(temp_dir) / "kvmfr0"
            shm_file.write_text("ready", encoding="utf-8")
            shm_file.chmod(0o644)
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_binary="/usr/local/bin/looking-glass-client",
                looking_glass_shm_file=shm_file,
                looking_glass_renderer="egl",
                looking_glass_fullscreen=False,
                looking_glass_disable_host_screensaver=False,
                looking_glass_spice_enabled=False,
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["running"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)

        self.assertEqual(
            actions,
            [
                {
                    "type": "connect_console",
                    "backend": "looking-glass",
                    "launcher": "/usr/local/bin/looking-glass-client",
                    "argv": [
                        "/usr/local/bin/looking-glass-client",
                        "-g",
                        "egl",
                        "-f",
                        str(shm_file),
                        "-s",
                    ],
                }
            ],
        )

    def test_prepare_runtime_creates_moonlight_workspace_and_portable_marker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))

            daemon.prepare_runtime()
            self.assertTrue(state_dir.is_dir())
            self.assertTrue((state_dir / "portable.dat").is_file())
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state_dir / "portable.dat").stat().st_mode & 0o777, 0o600)

    def test_running_vm_connects_with_moonlight_launch_contract(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_host="2001:db8::10",
                moonlight_base_port=48010,
                moonlight_state_dir=state_dir,
            )
            moonlight_commands: list[tuple[list[str], str | None]] = []

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                moonlight_commands.append((command, cwd))
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "[2001:db8::10]:48010", "--csv"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=moonlight_app_list_csv("Desktop"),
                        stderr="",
                    )
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            ready_actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            self.assertEqual(
                ready_actions,
                [
                    {"type": "display_power", "state": "on", "output": ""},
                    {"type": "show_waiting", "reason": "connecting"},
                ],
            )

            tick_actions = daemon.tick(now=start_time)

        self.assertEqual(
            moonlight_commands,
            [
                (["/usr/bin/moonlight", "--version"], None),
                (["/usr/bin/moonlight", "list", "[2001:db8::10]:48010", "--csv"], str(state_dir)),
            ],
        )
        self.assertEqual(
            tick_actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": [
                        "moonlight",
                        "stream",
                        "[2001:db8::10]:48010",
                        "Desktop",
                        "--display-mode",
                        "fullscreen",
                    ],
                }
            ],
        )

    def test_running_vm_reconnects_after_moonlight_exit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            moonlight_commands: list[tuple[list[str], str | None]] = []

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                moonlight_commands.append((command, cwd))
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=moonlight_app_list_csv("Desktop"),
                        stderr="",
                    )
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)

            first_actions = daemon.tick(now=start_time)
            daemon.handle_session_message(
                {"type": "console_started", "backend": "moonlight", "pid": 4321},
                now=start_time,
            )
            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "backend": "moonlight", "code": 1, "signal": 0},
                now=start_time,
            )
            reconnect_actions = daemon.tick(now=start_time + timedelta(milliseconds=1000))

        self.assertEqual(first_actions[0]["backend"], "moonlight")
        self.assertEqual(exit_actions, [{"type": "show_waiting", "reason": "reconnecting"}])
        self.assertEqual(
            reconnect_actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": [
                        "moonlight",
                        "stream",
                        "192.168.50.20",
                        "Desktop",
                        "--display-mode",
                        "fullscreen",
                    ],
                }
            ],
        )
        self.assertEqual(
            moonlight_commands,
            [
                (["/usr/bin/moonlight", "--version"], None),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
            ],
        )
        self.assertEqual(daemon.state.session_state, SessionState.REQUESTING_CONSOLE)

    def test_moonlight_pairing_waits_for_pin_approval_when_host_is_unpaired(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            moonlight_commands: list[tuple[list[str], str | None]] = []
            probe_calls: list[tuple[str, int, float]] = []

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                moonlight_commands.append((command, cwd))
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="not paired")
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            def fake_probe(host: str, port: int, timeout_s: float) -> None:
                probe_calls.append((host, port, timeout_s))

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=fake_probe,
                pin_generator=lambda: "1234",
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)
            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(probe_calls, [("192.168.50.20", 47989, 1.0)])
        self.assertEqual(
            actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": ["moonlight", "pair", "192.168.50.20", "--pin", "1234"],
                }
            ],
        )
        self.assertEqual(
            moonlight_commands,
            [
                (["/usr/bin/moonlight", "--version"], None),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
            ],
        )
        self.assertEqual(daemon.state.session_state, SessionState.WAITING_FOR_PAIRING)
        self.assertEqual(daemon.state.moonlight_pair_state, MoonlightPairState.PENDING_PIN_APPROVAL)
        self.assertEqual(daemon.state.moonlight_pair_pin, "1234")
        self.assertEqual(payload["session_state"], "waiting_for_pairing")
        self.assertEqual(payload["moonlight_pair_state"], "pending_pin_approval")
        self.assertEqual(payload["moonlight_pair_pin"], "1234")
        self.assertEqual(payload["moonlight_host"], "192.168.50.20")
        self.assertEqual(payload["moonlight_base_port"], 47989)

    def test_moonlight_pairing_completion_launches_console_while_pairing_ui_is_running(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            list_results = iter([1, 0])
            moonlight_commands: list[tuple[list[str], str | None]] = []

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                moonlight_commands.append((command, cwd))
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    result = next(list_results)
                    return SimpleNamespace(
                        returncode=result,
                        stdout=moonlight_app_list_csv("Desktop") if result == 0 else "",
                        stderr="",
                    )
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
                pin_generator=lambda: "1234",
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)

            first_actions = daemon.tick(now=start_time)
            daemon.handle_session_message(
                {"type": "console_started", "backend": "moonlight", "pid": 9004},
                now=start_time + timedelta(seconds=1),
            )
            second_actions = daemon.tick(now=start_time + timedelta(seconds=2))

        self.assertEqual(
            first_actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": ["moonlight", "pair", "192.168.50.20", "--pin", "1234"],
                }
            ],
        )
        self.assertEqual(
            second_actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": [
                        "moonlight",
                        "stream",
                        "192.168.50.20",
                        "Desktop",
                        "--display-mode",
                        "fullscreen",
                    ],
                }
            ],
        )
        self.assertEqual(daemon.state.moonlight_pair_state, MoonlightPairState.PAIRED)
        self.assertIsNone(daemon.state.moonlight_pair_pin)
        self.assertEqual(daemon.state.session_state, SessionState.REQUESTING_CONSOLE)
        self.assertEqual(
            moonlight_commands,
            [
                (["/usr/bin/moonlight", "--version"], None),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
            ],
        )

    def test_moonlight_pairing_ui_exit_restores_waiting_message_without_reissuing_pin(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            moonlight_commands: list[tuple[list[str], str | None]] = []

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                moonlight_commands.append((command, cwd))
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
                pin_generator=lambda: "1234",
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)

            first_actions = daemon.tick(now=start_time)
            daemon.handle_session_message(
                {"type": "console_started", "backend": "moonlight", "pid": 9004},
                now=start_time + timedelta(seconds=1),
            )
            exit_actions = daemon.handle_session_message(
                {"type": "console_exited", "backend": "moonlight", "code": 0, "signal": 0},
                now=start_time + timedelta(seconds=2),
            )
            second_actions = daemon.tick(now=start_time + timedelta(seconds=3))

        self.assertEqual(
            first_actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": ["moonlight", "pair", "192.168.50.20", "--pin", "1234"],
                }
            ],
        )
        self.assertEqual(
            exit_actions,
            [
                {
                    "type": "show_waiting",
                    "reason": "pairing_required",
                    "details": {
                        "backend": "moonlight",
                        "host": "192.168.50.20",
                        "pin": "1234",
                        "instructions": (
                            "Open the Sunshine web UI PIN page on the guest and enter this PIN."
                        ),
                    },
                }
            ],
        )
        self.assertEqual(second_actions, [])
        self.assertEqual(daemon.state.moonlight_pair_state, MoonlightPairState.PENDING_PIN_APPROVAL)
        self.assertEqual(daemon.state.moonlight_pair_pin, "1234")
        self.assertEqual(daemon.state.session_state, SessionState.WAITING_FOR_PAIRING)
        self.assertEqual(
            moonlight_commands,
            [
                (["/usr/bin/moonlight", "--version"], None),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
                (["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"], str(state_dir)),
            ],
        )

    def test_moonlight_pairing_console_keeps_waiting_state_while_running(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=lambda command, cwd=None, text=True, capture_output=True, check=False, **_: (
                    SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                    if command == ["/usr/bin/moonlight", "--version"]
                    else SimpleNamespace(returncode=1, stdout="", stderr="")
                ),
                tcp_connect_probe=lambda host, port, timeout_s: None,
                pin_generator=lambda: "1234",
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            daemon.tick(now=start_time)
            daemon.handle_session_message(
                {"type": "console_started", "backend": "moonlight", "pid": 9004},
                now=start_time + timedelta(seconds=1),
            )
            actions = daemon.tick(now=start_time + timedelta(seconds=2))

        self.assertEqual(actions, [])
        self.assertTrue(daemon.console_running)
        self.assertEqual(daemon.state.session_state, SessionState.WAITING_FOR_PAIRING)

    def test_moonlight_app_match_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_app="desktop",
                moonlight_state_dir=state_dir,
            )

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=moonlight_app_list_csv("Desktop"),
                        stderr="",
                    )
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)

        self.assertEqual(
            actions,
            [
                {
                    "type": "connect_console",
                    "backend": "moonlight",
                    "launcher": "moonlight",
                    "cwd": str(state_dir),
                    "argv": [
                        "moonlight",
                        "stream",
                        "192.168.50.20",
                        "desktop",
                        "--display-mode",
                        "fullscreen",
                    ],
                }
            ],
        )

    def test_moonlight_missing_app_enters_degraded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_app="Steam Big Picture",
                moonlight_state_dir=state_dir,
            )
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=lambda command, cwd=None, text=True, capture_output=True, check=False, **_: (
                    SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                    if command == ["/usr/bin/moonlight", "--version"]
                    else SimpleNamespace(
                        returncode=0,
                        stdout=moonlight_app_list_csv("Desktop", "Playnite"),
                        stderr="",
                    )
                ),
                tcp_connect_probe=lambda host, port, timeout_s: None,
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)
            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.moonlight_pair_state, MoonlightPairState.UNKNOWN)
        self.assertEqual(payload["moonlight_app"], "Steam Big Picture")
        self.assertEqual(
            daemon.state.degraded_reason,
            "Console preparation failed for backend=moonlight: "
            "Configured Moonlight app is not available on 192.168.50.20: Steam Big Picture",
        )

    def test_moonlight_pairing_timeout_reissues_pin(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            pins = iter(["1111", "2222"])

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=lambda host, port, timeout_s: None,
                pin_generator=lambda: next(pins),
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)

            first_actions = daemon.tick(now=start_time)
            second_actions = daemon.tick(now=start_time + timedelta(seconds=299))
            third_actions = daemon.tick(now=start_time + timedelta(seconds=300))

        self.assertEqual(first_actions[0]["argv"][-1], "1111")
        self.assertEqual(second_actions, [])
        self.assertEqual(third_actions[0]["argv"][-1], "2222")
        self.assertEqual(daemon.state.moonlight_pair_pin, "2222")

    def test_moonlight_pairing_clears_pin_when_host_becomes_unreachable(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "moonlight-state"
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_state_dir=state_dir,
            )
            host_probe_outcomes = iter([None, OSError("refused")])

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                if command == ["/usr/bin/moonlight", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="Moonlight 6.1.0\n", stderr="")
                if command == ["/usr/bin/moonlight", "list", "192.168.50.20", "--csv"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                raise AssertionError(f"Unexpected Moonlight command: {command}")

            def fake_probe(host: str, port: int, timeout_s: float) -> None:
                outcome = next(host_probe_outcomes)
                if isinstance(outcome, Exception):
                    raise outcome

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running", "running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
                tcp_connect_probe=fake_probe,
                pin_generator=lambda: "1234",
            )
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)

            daemon.tick(now=start_time)
            actions = daemon.tick(now=start_time + timedelta(seconds=2))

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "reconnecting"}])
        self.assertEqual(daemon.state.session_state, SessionState.RECONNECTING_CONSOLE)
        self.assertEqual(daemon.state.moonlight_pair_state, MoonlightPairState.UNKNOWN)
        self.assertIsNone(daemon.state.moonlight_pair_pin)
        self.assertEqual(
            daemon.state.last_error,
            "Moonlight host is not reachable yet: 192.168.50.20 (tcp/47989)",
        )

    def test_moonlight_missing_binary_enters_degraded_at_startup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="moonlight")
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: (
                    None if name == "moonlight" else f"/usr/bin/{Path(name).name}"
                ),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.degraded_reason, "Missing required binary: moonlight")

    def test_moonlight_old_version_enters_degraded_at_startup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="moonlight")

            def fake_command_runner(
                command: list[str],
                cwd: str | None = None,
                text: bool = True,
                capture_output: bool = True,
                check: bool = False,
                **_: object,
            ) -> SimpleNamespace:
                return SimpleNamespace(
                    returncode=0,
                    stdout="Moonlight 5.0.1\n",
                    stderr="",
                )

            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
                command_runner=fake_command_runner,
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(
            daemon.state.degraded_reason,
            "Moonlight version must be >= 6.0.0, found 5.0.1",
        )

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
                {
                    "type": "connect_console",
                    "backend": "spice",
                    "launcher": "remote-viewer",
                    "argv": [
                        "remote-viewer",
                        "--full-screen",
                        str(config.console.spice.vv_path),
                    ],
                },
            ],
        )
        self.assertEqual(daemon.state.display_power_intent, "on")
        self.assertEqual(daemon.state.session_state, SessionState.REQUESTING_CONSOLE)

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

    def test_power_button_start_wakes_display_while_vm_is_starting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True, debounce_ms=2000)
            proxmox = FakeProxmoxClient(["stopped", "starting"])
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
            daemon.session_ready = True
            daemon.state.display_power_intent = "off"
            daemon.state.display_power_applied = "off"
            daemon.state.vm_power_state = "stopped"
            daemon.state.session_state = SessionState.DISPLAY_SLEEPING

            actions = daemon.tick(now=start_time)

        self.assertEqual(actions, [{"type": "display_power", "state": "on", "output": ""}])
        self.assertEqual(proxmox.start_calls, [101])
        self.assertEqual(daemon.state.display_power_intent, "on")
        self.assertEqual(daemon.state.session_state, SessionState.WAITING_FOR_VM)
        self.assertTrue(daemon.state.power_button_action_in_flight)
        self.assertEqual(daemon.state.last_power_button_result, "submitted")

    def test_power_button_start_stays_in_flight_while_vm_is_starting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), forward_power_button=True, debounce_ms=2000)
            proxmox = FakeProxmoxClient(["stopped", "starting", "starting"])
            source = FakePowerButtonSource([1, 0])
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
            daemon.tick(now=start_time + timedelta(milliseconds=2500))

        self.assertEqual(proxmox.start_calls, [101])
        self.assertTrue(daemon.state.power_button_action_in_flight)
        self.assertEqual(daemon.state.last_power_button_result, "submitted")

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

    def test_missing_required_binary_enters_degraded_with_clear_reason(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: None if name == "remote-viewer" else f"/usr/bin/{name}",
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.degraded_reason, "Missing required binary: remote-viewer")

    def test_looking_glass_missing_binary_enters_degraded_at_startup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            shm_file = Path(temp_dir) / "kvmfr0"
            shm_file.write_text("ready", encoding="utf-8")
            shm_file.chmod(0o644)
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_shm_file=shm_file,
            )
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: (
                    None if name == "looking-glass-client" else f"/usr/bin/{Path(name).name}"
                ),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            with patch(
                "relayinner_display.daemon.pwd.getpwnam",
                return_value=SimpleNamespace(
                    pw_name="relayinner-display",
                    pw_uid=1001,
                    pw_gid=1001,
                ),
            ), patch("relayinner_display.daemon.grp.getgrall", return_value=[]):
                daemon.prepare_runtime()
                daemon.start(now=start_time)
                actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.degraded_reason, "Missing required binary: looking-glass-client")

    def test_vnc_endpoint_not_yet_reachable_enters_reconnect_flow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="vnc")
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(
                    ["running"],
                    vnc_probe_error=VncEndpointUnavailableError(
                        "VNC endpoint 127.0.0.1:5977 is not reachable yet: refused"
                    ),
                ),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "reconnecting"}])
        self.assertEqual(daemon.state.session_state, SessionState.RECONNECTING_CONSOLE)
        self.assertIsNone(daemon.state.degraded_reason)
        self.assertEqual(
            daemon.state.last_error,
            "VNC endpoint 127.0.0.1:5977 is not reachable yet: refused",
        )

    def test_vnc_config_mismatch_enters_degraded_with_clear_reason(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="vnc")
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(
                    ["running"],
                    vnc_validation_error=VncConfigurationError(
                        "VM config exposes VNC on non-loopback bind_host='0.0.0.0'"
                    ),
                ),
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            actions = daemon.tick(now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(
            daemon.state.degraded_reason,
            "Console preparation failed for backend=vnc: "
            "VM config exposes VNC on non-loopback bind_host='0.0.0.0'",
        )

    def test_looking_glass_missing_shm_file_enters_degraded_with_clear_reason(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_shm_file=Path(temp_dir) / "missing-kvmfr0",
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["running"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(
            daemon.state.degraded_reason,
            f"Looking Glass shared memory path does not exist: {Path(temp_dir) / 'missing-kvmfr0'}",
        )

    def test_looking_glass_unreadable_shm_file_enters_degraded_with_clear_reason(self) -> None:
        with TemporaryDirectory() as temp_dir:
            shm_file = Path(temp_dir) / "kvmfr0"
            shm_file.write_text("ready", encoding="utf-8")
            shm_file.chmod(0o600)
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_shm_file=shm_file,
            )
            daemon = DisplayDaemon(
                config=config,
                proxmox=FakeProxmoxClient(["running"]),
                dependency_finder=lambda name: f"/usr/bin/{Path(name).name}",
            )
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            with patch(
                "relayinner_display.daemon.pwd.getpwnam",
                return_value=SimpleNamespace(
                    pw_name="relayinner-display",
                    pw_uid=2000,
                    pw_gid=2000,
                ),
            ), patch("relayinner_display.daemon.grp.getgrall", return_value=[]):
                daemon.prepare_runtime()
                daemon.start(now=start_time)
                actions = daemon.handle_session_message({"type": "session_ready"}, now=start_time)

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(
            daemon.state.degraded_reason,
            "Looking Glass shared memory path is not readable by session user "
            f"relayinner-display: {shm_file}",
        )

    def test_repeated_proxmox_failures_enter_degraded_after_retry_budget(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            daemon = DisplayDaemon(config=config, proxmox=FailingProxmoxClient())
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.session_ready = True

            actions: list[dict[str, object]] = []
            for attempt in range(5):
                actions = daemon.tick(now=start_time + timedelta(seconds=attempt * 10))

        self.assertEqual(actions, [{"type": "show_waiting", "reason": "degraded"}])
        self.assertEqual(daemon.state.session_state, SessionState.DEGRADED)
        self.assertEqual(daemon.state.degraded_reason, "qm failed: missing VM")

    def test_state_file_includes_spec15_operational_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            proxmox = FakeProxmoxClient(["running", "running"])
            daemon = DisplayDaemon(config=config, proxmox=proxmox)
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)
            daemon.handle_session_message({"type": "session_ready"}, now=start_time)
            daemon.tick(now=start_time)
            daemon.handle_session_message(
                {"type": "console_started", "backend": "spice", "pid": 4321},
                now=start_time,
            )
            daemon.handle_session_message(
                {"type": "console_exited", "backend": "spice", "code": 1, "signal": 0},
                now=start_time + timedelta(seconds=1),
            )

            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["appliance_state"], "reconnecting_console")
        self.assertEqual(payload["console_backend"], "spice")
        self.assertIsNone(payload["active_console_backend"])
        self.assertIsNone(payload["vnc_endpoint"])
        self.assertTrue(payload["session_ready"])
        self.assertEqual(payload["vm_power_state"], "running")
        self.assertEqual(payload["display_power_applied"], "on")
        self.assertIsNone(payload["degraded_reason"])
        self.assertEqual(payload["last_console_exit"]["backend"], "spice")
        self.assertEqual(payload["last_console_exit"]["code"], 1)
        self.assertEqual(payload["last_console_exit"]["signal"], 0)

    def test_state_file_includes_vnc_endpoint_for_vnc_backend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir), backend="vnc")
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["console_backend"], "vnc")
        self.assertEqual(payload["vnc_endpoint"], "127.0.0.1:5977")

    def test_state_file_includes_looking_glass_shm_file_for_backend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            shm_file = Path(temp_dir) / "kvmfr0"
            shm_file.write_text("ready", encoding="utf-8")
            shm_file.chmod(0o644)
            config = build_config(
                Path(temp_dir),
                backend="looking-glass",
                looking_glass_shm_file=shm_file,
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            start_time = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["console_backend"], "looking-glass")
        self.assertEqual(payload["looking_glass_shm_file"], str(shm_file))

    def test_state_file_includes_moonlight_app_for_backend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(
                Path(temp_dir),
                backend="moonlight",
                moonlight_app="Steam Big Picture",
            )
            daemon = DisplayDaemon(config=config, proxmox=FakeProxmoxClient(["stopped"]))
            start_time = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)

            daemon.prepare_runtime()
            daemon.start(now=start_time)

            payload = json.loads(config.runtime.daemon_state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["console_backend"], "moonlight")
        self.assertEqual(payload["moonlight_app"], "Steam Big Picture")


def build_config(
    root: Path,
    *,
    backend: str = "spice",
    vnc_bind_host: str = "127.0.0.1",
    vnc_display_number: int = 77,
    vnc_viewer: str = "remote-viewer",
    looking_glass_binary: str = "looking-glass-client",
    looking_glass_shm_file: Path | None = None,
    looking_glass_renderer: str = "auto",
    looking_glass_fullscreen: bool = True,
    looking_glass_disable_host_screensaver: bool = True,
    looking_glass_spice_enabled: bool = True,
    moonlight_binary: str = "moonlight",
    moonlight_host: str = "192.168.50.20",
    moonlight_base_port: int = 47989,
    moonlight_app: str = "Desktop",
    moonlight_state_dir: Path | None = None,
    moonlight_quit_app_after_session: bool = False,
    output_name: str = "",
    power_helper: str = "wlr-randr",
    dpms_off_delay_ms: int = 5000,
    power_state_stabilize_ms: int = 3000,
    forward_power_button: bool = False,
    debounce_ms: int = 2000,
) -> AppConfig:
    run_dir = root / "run"
    console = ConsoleConfig(
        artifact_dir=run_dir / "console",
        spice=ConsoleSpiceConfig(vv_path=run_dir / "console" / "spice-current.vv")
        if backend == "spice"
        else None,
        vnc=ConsoleVncConfig(
            display_number=vnc_display_number,
            bind_host=vnc_bind_host,
            viewer=vnc_viewer,
        )
        if backend == "vnc"
        else None,
        looking_glass=ConsoleLookingGlassConfig(
            binary=looking_glass_binary,
            shm_file=looking_glass_shm_file or root / "kvmfr0",
            renderer=looking_glass_renderer,
            fullscreen=looking_glass_fullscreen,
            disable_host_screensaver=looking_glass_disable_host_screensaver,
            spice_enabled=looking_glass_spice_enabled,
        )
        if backend == "looking-glass"
        else None,
        moonlight=ConsoleMoonlightConfig(
            binary=moonlight_binary,
            host=moonlight_host,
            base_port=moonlight_base_port,
            app=moonlight_app,
            state_dir=moonlight_state_dir or root / "moonlight-state",
            quit_app_after_session=moonlight_quit_app_after_session,
        )
        if backend == "moonlight"
        else None,
    )
    return AppConfig(
        target=TargetConfig(
            vmid=101,
            node_name="auto",
            guest_os="windows",
            console_backend=backend,
        ),
        runtime=RuntimeConfig(
            run_dir=run_dir,
            control_socket=run_dir / "session.sock",
            spice_vv_path=run_dir / "console" / "spice-current.vv",
            log_namespace="relayinner-display",
        ),
        console=console,
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
