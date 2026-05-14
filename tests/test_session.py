from __future__ import annotations

from pathlib import Path
import runpy
import socket
import subprocess
import sys
import unittest
import warnings
from unittest.mock import patch

from relayinner_display.config import (
    AppConfig,
    ConfigError,
    ConsoleConfig,
    ConsoleLookingGlassConfig,
    ConsoleMoonlightConfig,
    ConsoleSpiceConfig,
    ConsoleVncConfig,
    DisplayConfig,
    InputConfig,
    KioskConfig,
    PolicyConfig,
    RuntimeConfig,
    TargetConfig,
    resolve_kiosk_compositor,
)
from relayinner_display.session import (
    SessionSocketClient,
    SessionSupervisor,
    main,
    parse_wlr_randr_outputs,
    run,
)


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


class FakeStubbornProcess(FakeProcess):
    def __init__(self, pid: int = 3333) -> None:
        super().__init__(pid=pid)
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd="remote-viewer", timeout=timeout)
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class FakeUnkillableProcess(FakeStubbornProcess):
    def wait(self, timeout: int) -> int:
        self.wait_calls += 1
        raise subprocess.TimeoutExpired(cmd="remote-viewer", timeout=timeout)


class FakeSocket:
    def __init__(
        self,
        chunks: list[bytes | BaseException],
        *,
        send_error: BaseException | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.send_error = send_error
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            raise BlockingIOError
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, payload: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class WlrRandrOutputParsingTests(unittest.TestCase):
    def test_parse_outputs_ignores_invalid_names_and_duplicates(self) -> None:
        output = """\
HDMI-A-1 "Built-in display"
  Enabled: yes
bad:name "Invalid connector"
DP-1 "External"
DP-1 "Duplicate external"
Virtual_2.Description extra data
"""

        self.assertEqual(parse_wlr_randr_outputs(output), ["HDMI-A-1", "DP-1", "Virtual_2.Description"])


class FakeConnectingSocket:
    def __init__(self) -> None:
        self.connected_path: str | None = None
        self.blocking: bool | None = None

    def connect(self, path: str) -> None:
        self.connected_path = path

    def setblocking(self, blocking: bool) -> None:
        self.blocking = blocking


class SessionSocketClientTests(unittest.TestCase):
    def test_connect_opens_nonblocking_unix_socket_once(self) -> None:
        created_sockets: list[FakeConnectingSocket] = []

        def fake_socket(family: int, socket_type: int) -> FakeConnectingSocket:
            self.assertEqual(family, socket.AF_UNIX)
            self.assertEqual(socket_type, socket.SOCK_STREAM)
            created_socket = FakeConnectingSocket()
            created_sockets.append(created_socket)
            return created_socket

        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))

        with patch("relayinner_display.session.socket.socket", side_effect=fake_socket):
            client.connect()
            client.connect()

        self.assertEqual(len(created_sockets), 1)
        self.assertIs(client.connection, created_sockets[0])
        self.assertEqual(created_sockets[0].connected_path, "/run/relayinner-display/session.sock")
        self.assertFalse(created_sockets[0].blocking)

    def test_read_messages_without_connection_reports_disconnected(self) -> None:
        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))

        messages, disconnected = client.read_messages()

        self.assertEqual(messages, [])
        self.assertTrue(disconnected)

    def test_send_message_serializes_payload_and_clears_connection_after_write_error(self) -> None:
        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))
        self.assertFalse(client.send_message({"type": "session_ready"}))

        sending_socket = FakeSocket([])
        client.connection = sending_socket

        self.assertTrue(client.send_message({"type": "session_ready"}))
        self.assertEqual(sending_socket.sent, [b'{"type":"session_ready"}\n'])

        failing_socket = FakeSocket([], send_error=BrokenPipeError("closed"))
        client.connection = failing_socket
        client.buffer = b"partial"

        self.assertFalse(client.send_message({"type": "session_ready"}))
        self.assertTrue(failing_socket.closed)
        self.assertIsNone(client.connection)
        self.assertEqual(client.buffer, b"")

    def test_read_messages_treats_receive_errors_as_disconnect(self) -> None:
        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))
        failing_socket = FakeSocket([ConnectionResetError("reset")])
        client.connection = failing_socket
        client.buffer = b'{"type":"health_ping"'

        messages, disconnected = client.read_messages()

        self.assertEqual(messages, [])
        self.assertTrue(disconnected)
        self.assertTrue(failing_socket.closed)
        self.assertIsNone(client.connection)
        self.assertEqual(client.buffer, b"")

    def test_read_messages_ignores_blank_frames_and_continues_parsing(self) -> None:
        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))
        socket_with_blank_frames = FakeSocket([b'\n  \n{"type":"health_ping"}\n'])
        client.connection = socket_with_blank_frames

        messages, disconnected = client.read_messages()

        self.assertEqual(messages, [{"type": "health_ping"}])
        self.assertFalse(disconnected)
        self.assertFalse(socket_with_blank_frames.closed)
        self.assertEqual(client.buffer, b"")

    def test_read_messages_buffers_partial_frames_until_newline_and_detects_disconnect(self) -> None:
        client = SessionSocketClient(Path("/run/relayinner-display/session.sock"))
        first_socket = FakeSocket([b'{"type":"health_ping"'])
        client.connection = first_socket  # exercise read buffering without opening a real socket

        messages, disconnected = client.read_messages()

        self.assertEqual(messages, [])
        self.assertFalse(disconnected)
        self.assertEqual(client.buffer, b'{"type":"health_ping"')
        self.assertFalse(first_socket.closed)

        second_socket = FakeSocket([b'}\n{"type":"disconnect_console","reason":"vm_stopped"}\n', b""])
        client.connection = second_socket

        messages, disconnected = client.read_messages()

        self.assertEqual(
            messages,
            [
                {"type": "health_ping"},
                {"type": "disconnect_console", "reason": "vm_stopped"},
            ],
        )
        self.assertTrue(disconnected)
        self.assertTrue(second_socket.closed)
        self.assertIsNone(client.connection)
        self.assertEqual(client.buffer, b"")


class SessionSupervisorTests(unittest.TestCase):
    def test_session_ready_message_announces_session_without_extra_payload(self) -> None:
        supervisor = SessionSupervisor(config=build_config())

        self.assertEqual(supervisor.session_ready_message(), {"type": "session_ready"})

    def test_unhandled_daemon_message_type_raises_assertion(self) -> None:
        supervisor = SessionSupervisor(config=build_config())

        with (
            patch("relayinner_display.session.validate_daemon_message", return_value={"type": "mystery"}),
            self.assertRaisesRegex(AssertionError, "Unhandled daemon message type: mystery"),
        ):
            supervisor.handle_daemon_message({"type": "health_ping"})

    def test_connect_console_launches_remote_viewer_for_spice(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )

        self.assertEqual(events, [{"type": "console_started", "backend": "spice", "pid": 9001}])
        self.assertTrue(supervisor.view_state.console_active)
        self.assertTrue(supervisor.view_state.cursor_hidden)
        self.assertEqual(supervisor.view_state.status_text, "Connecting")
        self.assertEqual(
            launches[0][0],
            ["remote-viewer", "--full-screen", "/run/relayinner-display/console/spice-current.vv"],
        )

    def test_connect_console_launches_remote_viewer_for_vnc(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9002)

        supervisor = SessionSupervisor(
            config=build_config(backend="vnc"),
            process_factory=fake_factory,
        )
        events = supervisor.handle_daemon_message(
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
        )

        self.assertEqual(events, [{"type": "console_started", "backend": "vnc", "pid": 9002}])
        self.assertEqual(
            launches[0][0],
            ["remote-viewer", "--full-screen", "vnc://127.0.0.1:5977"],
        )

    def test_connect_console_launches_looking_glass_client(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9003)

        supervisor = SessionSupervisor(
            config=build_config(backend="looking-glass"),
            process_factory=fake_factory,
        )
        events = supervisor.handle_daemon_message(
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
                    "/dev/kvmfr0",
                ],
            }
        )

        self.assertEqual(
            events,
            [{"type": "console_started", "backend": "looking-glass", "pid": 9003}],
        )
        self.assertEqual(
            launches[0][0],
            ["looking-glass-client", "-F", "-S", "-g", "auto", "-f", "/dev/kvmfr0"],
        )

    def test_connect_console_launches_moonlight_from_workspace(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9004)

        supervisor = SessionSupervisor(
            config=build_config(backend="moonlight"),
            process_factory=fake_factory,
        )
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "moonlight",
                "launcher": "moonlight",
                "cwd": "/var/lib/relayinner-display/moonlight",
                "argv": [
                    "moonlight",
                    "stream",
                    "192.168.50.20",
                    "Desktop",
                    "--display-mode",
                    "fullscreen",
                ],
            }
        )

        self.assertEqual(events, [{"type": "console_started", "backend": "moonlight", "pid": 9004}])
        self.assertEqual(
            launches[0][0],
            [
                "moonlight",
                "stream",
                "192.168.50.20",
                "Desktop",
                "--display-mode",
                "fullscreen",
            ],
        )
        self.assertEqual(launches[0][1], "/var/lib/relayinner-display/moonlight")

    def test_connect_console_launches_moonlight_with_quit_after_when_enabled(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9005)

        config = build_config(
            backend="moonlight",
            moonlight_app="Steam Big Picture",
            moonlight_quit_app_after_session=True,
        )
        supervisor = SessionSupervisor(config=config, process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "moonlight",
                "launcher": "moonlight",
                "cwd": "/var/lib/relayinner-display/moonlight",
                "argv": config.console.moonlight.argv,
            }
        )

        self.assertEqual(events, [{"type": "console_started", "backend": "moonlight", "pid": 9005}])
        self.assertEqual(
            launches[0][0],
            [
                "moonlight",
                "stream",
                "192.168.50.20",
                "Steam Big Picture",
                "--display-mode",
                "fullscreen",
                "--quit-after",
            ],
        )

    def test_connect_console_launches_moonlight_with_resolution_override(self) -> None:
        launches: list[tuple[list[str], str | None, dict[str, str]]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append((command, cwd, env or {}))
            return FakeProcess(pid=9006)

        config = build_config(
            backend="moonlight",
            moonlight_app="Playnite",
            moonlight_resolution="1920x1080",
        )
        supervisor = SessionSupervisor(config=config, process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "moonlight",
                "launcher": "moonlight",
                "cwd": "/var/lib/relayinner-display/moonlight",
                "argv": config.console.moonlight.argv,
            }
        )

        self.assertEqual(events, [{"type": "console_started", "backend": "moonlight", "pid": 9006}])
        self.assertEqual(
            launches[0][0],
            [
                "moonlight",
                "stream",
                "192.168.50.20",
                "Playnite",
                "--resolution",
                "1920x1080",
                "--display-mode",
                "fullscreen",
            ],
        )

    def test_connect_spice_compatibility_emits_legacy_console_started(self) -> None:
        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        )

        self.assertEqual(events, [{"type": "console_started", "pid": 9001}])

    def test_connect_spice_compatibility_reports_legacy_console_exit(self) -> None:
        process = FakeProcess(pid=9001)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {"type": "connect_spice", "vv_path": "/run/relayinner-display/current.vv"}
        )
        process.returncode = -9

        event = supervisor.poll_console()

        self.assertEqual(event, {"type": "console_exited", "code": 0, "signal": 9})
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")

    def test_connect_console_reports_viewer_launch_failure(self) -> None:
        def failing_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            raise FileNotFoundError("remote-viewer")

        supervisor = SessionSupervisor(config=build_config(), process_factory=failing_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": "viewer_launch_failed: backend=spice: remote-viewer",
                }
            ],
        )
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Degraded")

    def test_connect_console_rejects_non_allowlisted_launcher(self) -> None:
        launches: list[list[str]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append(command)
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(
            config=build_config(backend="vnc"),
            process_factory=fake_factory,
        )
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "vnc",
                "launcher": "looking-glass-client",
                "argv": ["looking-glass-client"],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": (
                        "invalid_console_request: "
                        "backend=vnc launcher=looking-glass-client argv0=looking-glass-client"
                    ),
                }
            ],
        )
        self.assertEqual(launches, [])
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Degraded")

    def test_connect_console_rejects_backend_that_does_not_match_config(self) -> None:
        launches: list[list[str]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append(command)
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(config=build_config(backend="spice"), process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "vnc",
                "launcher": "remote-viewer",
                "argv": ["remote-viewer", "--full-screen", "vnc://127.0.0.1:5977"],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": (
                        "invalid_console_request: "
                        "backend=vnc does not match configured backend=spice"
                    ),
                }
            ],
        )
        self.assertEqual(launches, [])
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Degraded")

    def test_connect_console_rejects_unsupported_configured_backend(self) -> None:
        base_config = build_config(backend="spice")
        config = AppConfig(
            target=TargetConfig(
                vmid=base_config.target.vmid,
                node_name=base_config.target.node_name,
                guest_os=base_config.target.guest_os,
                console_backend="serial",
            ),
            runtime=base_config.runtime,
            console=ConsoleConfig(artifact_dir=base_config.console.artifact_dir),
            display=base_config.display,
            kiosk=base_config.kiosk,
            input=base_config.input,
            policy=base_config.policy,
        )
        launches: list[list[str]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append(command)
            return FakeProcess(pid=9001)

        supervisor = SessionSupervisor(config=config, process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "serial",
                "launcher": "serial-client",
                "argv": ["serial-client"],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": "invalid_console_request: backend=serial is not supported",
                }
            ],
        )
        self.assertEqual(launches, [])
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Degraded")

    def test_connect_console_rejects_moonlight_argv0_mismatch(self) -> None:
        launches: list[list[str]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append(command)
            return FakeProcess(pid=9005)

        supervisor = SessionSupervisor(
            config=build_config(
                backend="moonlight",
                moonlight_binary="/usr/local/bin/moonlight",
            ),
            process_factory=fake_factory,
        )
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "moonlight",
                "launcher": "/usr/local/bin/moonlight",
                "cwd": "/var/lib/relayinner-display/moonlight",
                "argv": [
                    "moonlight",
                    "stream",
                    "192.168.50.20",
                    "Desktop",
                    "--display-mode",
                    "fullscreen",
                ],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": (
                        "invalid_console_request: "
                        "backend=moonlight launcher=/usr/local/bin/moonlight argv0=moonlight"
                    ),
                }
            ],
        )
        self.assertEqual(launches, [])

    def test_connect_console_rejects_moonlight_when_not_configured(self) -> None:
        base_config = build_config(backend="moonlight")
        config = AppConfig(
            target=base_config.target,
            runtime=base_config.runtime,
            console=ConsoleConfig(artifact_dir=base_config.console.artifact_dir),
            display=base_config.display,
            kiosk=base_config.kiosk,
            input=base_config.input,
            policy=base_config.policy,
        )
        launches: list[list[str]] = []

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            launches.append(command)
            return FakeProcess(pid=9005)

        supervisor = SessionSupervisor(config=config, process_factory=fake_factory)
        events = supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "moonlight",
                "launcher": "moonlight",
                "cwd": "/var/lib/relayinner-display/moonlight",
                "argv": [
                    "moonlight",
                    "stream",
                    "192.168.50.20",
                    "Desktop",
                    "--display-mode",
                    "fullscreen",
                ],
            }
        )

        self.assertEqual(
            events,
            [
                {
                    "type": "session_error",
                    "reason": "invalid_console_request: backend=moonlight is not configured",
                }
            ],
        )
        self.assertEqual(launches, [])
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Degraded")

    def test_intentional_disconnect_suppresses_console_exit_event(self) -> None:
        process = FakeProcess(pid=100)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )
        supervisor.handle_daemon_message({"type": "show_waiting", "reason": "vm_stopped"})

        self.assertTrue(process.terminated)
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")
        self.assertIsNone(supervisor.poll_console())

    def test_poll_console_suppresses_pending_exit_report_once(self) -> None:
        process = FakeProcess(pid=107)
        process.returncode = 0
        supervisor = SessionSupervisor(config=build_config())
        supervisor.console_process = process
        supervisor.active_console_backend = "spice"
        supervisor._suppress_exit_report = True
        supervisor._active_console_uses_legacy_events = True

        self.assertIsNone(supervisor.poll_console())

        self.assertFalse(supervisor._suppress_exit_report)
        self.assertFalse(supervisor._active_console_uses_legacy_events)
        self.assertIsNone(supervisor.console_process)
        self.assertIsNone(supervisor.active_console_backend)
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")

    def test_show_waiting_kills_console_after_terminate_timeout(self) -> None:
        process = FakeStubbornProcess(pid=105)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )

        events = supervisor.handle_daemon_message({"type": "show_waiting", "reason": "vm_stopped"})

        self.assertEqual(events, [])
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")
        self.assertIsNone(supervisor.poll_console())

    def test_show_waiting_clears_console_when_process_stays_alive_after_kill(self) -> None:
        process = FakeUnkillableProcess(pid=106)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )

        events = supervisor.handle_daemon_message({"type": "show_waiting", "reason": "vm_stopped"})

        self.assertEqual(events, [])
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")
        self.assertIsNone(supervisor.poll_console())

    def test_show_waiting_pairing_details_update_view_state(self) -> None:
        supervisor = SessionSupervisor(config=build_config(backend="moonlight"))

        events = supervisor.handle_daemon_message(
            {
                "type": "show_waiting",
                "reason": "pairing_required",
                "details": {
                    "backend": "moonlight",
                    "host": "192.168.50.20",
                    "pin": "1234",
                    "instructions": "Open the Sunshine web UI PIN page on the guest and enter this PIN.",
                },
            }
        )

        self.assertEqual(events, [])
        self.assertEqual(supervisor.view_state.status_text, "Pairing required")
        self.assertEqual(
            supervisor.view_state.details,
            {
                "backend": "moonlight",
                "host": "192.168.50.20",
                "pin": "1234",
                "instructions": "Open the Sunshine web UI PIN page on the guest and enter this PIN.",
            },
        )

    def test_health_ping_is_ignored_without_changing_view_state(self) -> None:
        supervisor = SessionSupervisor(config=build_config())
        supervisor.view_state.waiting_reason = "reconnecting"
        supervisor.view_state.status_text = "Connection lost"

        events = supervisor.handle_daemon_message({"type": "health_ping"})

        self.assertEqual(events, [])
        self.assertEqual(supervisor.view_state.waiting_reason, "reconnecting")
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")
        self.assertIsNone(supervisor.view_state.details)
        self.assertFalse(supervisor.view_state.console_active)

    def test_disconnect_console_clears_waiting_details_and_shows_reason(self) -> None:
        supervisor = SessionSupervisor(config=build_config(backend="moonlight"))
        supervisor.handle_daemon_message(
            {
                "type": "show_waiting",
                "reason": "pairing_required",
                "details": {
                    "backend": "moonlight",
                    "host": "192.168.50.20",
                    "pin": "1234",
                },
            }
        )

        events = supervisor.handle_daemon_message(
            {"type": "disconnect_console", "reason": "reconnecting"}
        )

        self.assertEqual(events, [])
        self.assertEqual(supervisor.view_state.waiting_reason, "reconnecting")
        self.assertIsNone(supervisor.view_state.details)
        self.assertFalse(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")

    def test_poll_console_keeps_running_console_active_without_exit_event(self) -> None:
        process = FakeProcess(pid=104)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )

        event = supervisor.poll_console()

        self.assertIsNone(event)
        self.assertFalse(process.terminated)
        self.assertTrue(supervisor.view_state.console_active)
        self.assertEqual(supervisor.view_state.status_text, "Connecting")

    def test_unexpected_exit_reports_console_exited_with_backend(self) -> None:
        process = FakeProcess(pid=100)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(config=build_config(), process_factory=fake_factory)
        supervisor.handle_daemon_message(
            {
                "type": "connect_console",
                "backend": "spice",
                "launcher": "remote-viewer",
                "argv": [
                    "remote-viewer",
                    "--full-screen",
                    "/run/relayinner-display/console/spice-current.vv",
                ],
            }
        )
        process.returncode = 1

        event = supervisor.poll_console()

        self.assertEqual(
            event,
            {"type": "console_exited", "backend": "spice", "code": 1, "signal": 0},
        )
        self.assertEqual(supervisor.view_state.status_text, "Connection lost")

    def test_unexpected_looking_glass_exit_reports_console_exited_with_backend(self) -> None:
        process = FakeProcess(pid=101)

        def fake_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return process

        supervisor = SessionSupervisor(
            config=build_config(backend="looking-glass"),
            process_factory=fake_factory,
        )
        supervisor.handle_daemon_message(
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
                    "/dev/kvmfr0",
                ],
            }
        )
        process.returncode = 1

        event = supervisor.poll_console()

        self.assertEqual(
            event,
            {"type": "console_exited", "backend": "looking-glass", "code": 1, "signal": 0},
        )
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

    def test_display_power_with_wlr_randr_uses_output_flag(self) -> None:
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
            config=build_config(power_helper="wlr-randr"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": "HDMI-A-1"}
        )

        self.assertEqual(events, [{"type": "display_power_applied", "state": "off"}])
        self.assertEqual(commands[0][0], ["wlr-randr", "--output", "HDMI-A-1", "--off"])
        self.assertEqual(supervisor.view_state.status_text, "Display sleeping")

    def test_display_power_with_wlr_randr_lists_outputs_when_unpinned(self) -> None:
        commands: list[tuple[list[str], dict[str, str]]] = []

        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            commands.append((command, env))
            if command == ["wlr-randr"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    'HDMI-A-1 "Built-in display"\n  Enabled: yes\nDP-1 "External"\n  Enabled: yes\n',
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        supervisor = SessionSupervisor(
            config=build_config(power_helper="wlr-randr"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": ""}
        )

        self.assertEqual(events, [{"type": "display_power_applied", "state": "off"}])
        self.assertEqual(commands[0][0], ["wlr-randr"])
        self.assertEqual(commands[1][0], ["wlr-randr", "--output", "HDMI-A-1", "--off"])
        self.assertEqual(commands[2][0], ["wlr-randr", "--output", "DP-1", "--off"])
        self.assertEqual(supervisor.view_state.status_text, "Display sleeping")

    def test_display_power_with_wlr_randr_stops_when_output_command_fails(self) -> None:
        commands: list[list[str]] = []

        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command == ["wlr-randr"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    'HDMI-A-1 "Built-in display"\nDP-1 "External"\n',
                    "",
                )
            if command == ["wlr-randr", "--output", "DP-1", "--off"]:
                return subprocess.CompletedProcess(command, 2, "", "link lost")
            return subprocess.CompletedProcess(command, 0, "", "")

        supervisor = SessionSupervisor(
            config=build_config(power_helper="wlr-randr"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": ""}
        )

        self.assertEqual(events, [])
        self.assertEqual(
            commands,
            [
                ["wlr-randr"],
                ["wlr-randr", "--output", "HDMI-A-1", "--off"],
                ["wlr-randr", "--output", "DP-1", "--off"],
            ],
        )
        self.assertEqual(supervisor.view_state.display_power_state, "on")
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")

    def test_display_power_with_wlr_randr_no_outputs_is_nonfatal(self) -> None:
        commands: list[list[str]] = []

        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "  Enabled: yes\n", "")

        supervisor = SessionSupervisor(
            config=build_config(power_helper="wlr-randr"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": ""}
        )

        self.assertEqual(events, [])
        self.assertEqual(commands, [["wlr-randr"]])
        self.assertEqual(supervisor.view_state.display_power_state, "on")
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")

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

    def test_display_power_helper_command_failure_is_nonfatal(self) -> None:
        commands: list[list[str]] = []

        def fake_power_runner(
            command: list[str],
            env: dict[str, str],
            text: bool,
            capture_output: bool,
            check: bool,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 2, "", "output missing")

        supervisor = SessionSupervisor(
            config=build_config(power_helper="relay-wlopm"),
            power_command_runner=fake_power_runner,
        )

        events = supervisor.handle_daemon_message(
            {"type": "display_power", "state": "off", "output": "HDMI-A-1"}
        )

        self.assertEqual(events, [])
        self.assertEqual(commands, [["relay-wlopm", "--off", "HDMI-A-1"]])
        self.assertEqual(supervisor.view_state.display_power_state, "on")
        self.assertEqual(supervisor.view_state.status_text, "Waiting for VM")


class SessionRuntimeTests(unittest.TestCase):
    def test_run_retries_after_control_socket_connection_failure(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []

        class FailingSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise OSError("daemon unavailable")

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("failed connects are not made nonblocking")

        sleep_delays: list[float] = []

        def fake_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=FailingSocket()),
            patch("relayinner_display.session.time.sleep", side_effect=fake_sleep),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertEqual(sleep_delays, [2.0])

    def test_run_continues_after_control_socket_connection_retry_delay(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class FailingSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise OSError("daemon unavailable")

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("failed connects are not made nonblocking")

        class StopLoopSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise StopLoop

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("stopped before nonblocking setup")

        def fake_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", side_effect=[FailingSocket(), StopLoopSocket()]),
            patch("relayinner_display.session.time.sleep", side_effect=fake_sleep),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(
            observed_paths,
            [
                "/run/relayinner-display/session.sock",
                "/run/relayinner-display/session.sock",
            ],
        )
        self.assertEqual(sleep_delays, [2.0])

    def test_run_retries_when_session_ready_send_fails(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class ReadySendFailingSocket:
            def __init__(self) -> None:
                self.blocking: bool | None = None
                self.sent_payloads: list[bytes] = []
                self.closed = False

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

            def sendall(self, payload: bytes) -> None:
                self.sent_payloads.append(payload)
                raise BrokenPipeError("daemon closed")

            def close(self) -> None:
                self.closed = True

        connected_socket = ReadySendFailingSocket()

        def fake_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=fake_sleep),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(connected_socket.sent_payloads, [b'{"type":"session_ready"}\n'])
        self.assertTrue(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_reconnects_after_session_ready_send_retry_delay(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class ReadySendFailingSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__([])
                self.blocking: bool | None = None

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

            def sendall(self, payload: bytes) -> None:
                self.sent.append(payload)
                raise BrokenPipeError("daemon closed")

        class StopOnConnectSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise StopLoop

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("stopped before nonblocking setup")

        first_socket = ReadySendFailingSocket()

        def fake_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", side_effect=[first_socket, StopOnConnectSocket()]),
            patch("relayinner_display.session.time.sleep", side_effect=fake_sleep),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(
            observed_paths,
            ["/run/relayinner-display/session.sock", "/run/relayinner-display/session.sock"],
        )
        self.assertFalse(first_socket.blocking)
        self.assertEqual(first_socket.sent, [b'{"type":"session_ready"}\n'])
        self.assertTrue(first_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_keeps_connection_open_after_health_ping(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class HealthPingSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__([b'{"type":"health_ping"}\n'])
                self.blocking: bool | None = None

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

        connected_socket = HealthPingSocket()

        def stop_after_inner_poll(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_inner_poll),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(connected_socket.sent, [b'{"type":"session_ready"}\n'])
        self.assertFalse(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_forwards_daemon_message_responses(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class InvalidConsoleRequestSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__(
                    [
                        b'{"type":"connect_console","backend":"vnc",'
                        b'"launcher":"remote-viewer","argv":["remote-viewer"]}\n'
                    ]
                )
                self.blocking: bool | None = None

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

        connected_socket = InvalidConsoleRequestSocket()

        def stop_after_response_poll(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_response_poll),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(
            connected_socket.sent,
            [
                b'{"type":"session_ready"}\n',
                (
                    b'{"reason":"invalid_console_request: backend=vnc does not match configured '
                    b'backend=spice","type":"session_error"}\n'
                ),
            ],
        )
        self.assertFalse(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_closes_socket_and_retries_after_response_send_failure(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class ResponseSendFailingSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__(
                    [
                        b'{"type":"connect_console","backend":"vnc",'
                        b'"launcher":"remote-viewer","argv":["remote-viewer"]}\n'
                    ]
                )
                self.blocking: bool | None = None
                self.send_attempts = 0

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

            def sendall(self, payload: bytes) -> None:
                self.sent.append(payload)
                self.send_attempts += 1
                if self.send_attempts == 2:
                    raise BrokenPipeError("daemon closed")

        connected_socket = ResponseSendFailingSocket()

        def stop_after_response_send_failure(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_response_send_failure),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(
            connected_socket.sent,
            [
                b'{"type":"session_ready"}\n',
                (
                    b'{"reason":"invalid_console_request: backend=vnc does not match configured '
                    b'backend=spice","type":"session_error"}\n'
                ),
            ],
        )
        self.assertTrue(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_closes_socket_and_retries_after_exit_event_send_failure(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []
        launched_process = FakeProcess(pid=9010)
        launched_process.returncode = 1

        class ExitEventSendFailingSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__(
                    [
                        b'{"type":"connect_console","backend":"spice",'
                        b'"launcher":"remote-viewer","argv":["remote-viewer","--full-screen",'
                        b'"/run/relayinner-display/console/spice-current.vv"]}\n'
                    ]
                )
                self.blocking: bool | None = None
                self.send_attempts = 0

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

            def sendall(self, payload: bytes) -> None:
                self.sent.append(payload)
                self.send_attempts += 1
                if self.send_attempts == 3:
                    raise BrokenPipeError("daemon closed")

        def fake_process_factory(
            command: list[str],
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            text: bool = True,
        ) -> FakeProcess:
            return launched_process

        def supervisor_factory(config: AppConfig, logger: object) -> SessionSupervisor:
            return SessionSupervisor(config=config, logger=logger, process_factory=fake_process_factory)

        connected_socket = ExitEventSendFailingSocket()

        def stop_after_exit_event_send_failure(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.SessionSupervisor", side_effect=supervisor_factory),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_exit_event_send_failure),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(
            connected_socket.sent,
            [
                b'{"type":"session_ready"}\n',
                b'{"backend":"spice","pid":9010,"type":"console_started"}\n',
                b'{"backend":"spice","code":1,"signal":0,"type":"console_exited"}\n',
            ],
        )
        self.assertTrue(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_closes_socket_and_retries_after_daemon_disconnect(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []
        sleep_delays: list[float] = []

        class DisconnectingSocket(FakeSocket):
            def __init__(self) -> None:
                super().__init__([b""])
                self.blocking: bool | None = None

            def connect(self, path: str) -> None:
                observed_paths.append(path)

            def setblocking(self, blocking: bool) -> None:
                self.blocking = blocking

        connected_socket = DisconnectingSocket()

        def stop_after_disconnect_retry(delay: float) -> None:
            sleep_delays.append(delay)
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", return_value=build_config()),
            patch("relayinner_display.session.socket.socket", return_value=connected_socket),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_disconnect_retry),
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertFalse(connected_socket.blocking)
        self.assertEqual(connected_socket.sent, [b'{"type":"session_ready"}\n'])
        self.assertTrue(connected_socket.closed)
        self.assertEqual(sleep_delays, [2.0])

    def test_run_logs_invalid_config_and_retries_with_fallback_socket(self) -> None:
        class StopLoop(Exception):
            pass

        observed_paths: list[str] = []

        class FailingSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise OSError("daemon unavailable")

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("failed connects are not made nonblocking")

        def stop_after_retry(delay: float) -> None:
            raise StopLoop

        with (
            patch("relayinner_display.session.load_config", side_effect=ConfigError("broken config")),
            patch("relayinner_display.session.socket.socket", return_value=FailingSocket()),
            patch("relayinner_display.session.time.sleep", side_effect=stop_after_retry),
            self.assertLogs("relayinner-display.session", level="ERROR") as logs,
            self.assertRaises(StopLoop),
        ):
            run(Path("/tmp/broken-relayinner-session.toml"))

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])
        self.assertEqual(logs.output, ["ERROR:relayinner-display.session:Invalid config: broken config"])


class SessionCliTests(unittest.TestCase):
    def test_main_forwards_explicit_config_path_to_session_runner(self) -> None:
        with patch("relayinner_display.session.run", return_value=7) as run_mock:
            result = main(["--config", "/tmp/relayinner-session.toml"])

        self.assertEqual(result, 7)
        run_mock.assert_called_once_with(Path("/tmp/relayinner-session.toml"))

    def test_module_entrypoint_dispatches_to_main(self) -> None:
        class StopEntrypoint(Exception):
            pass

        observed_paths: list[str] = []

        class StopOnConnectSocket:
            def connect(self, path: str) -> None:
                observed_paths.append(path)
                raise StopEntrypoint

            def setblocking(self, blocking: bool) -> None:
                raise AssertionError("stopped before nonblocking setup")

        with (
            patch("relayinner_display.config.load_config", return_value=build_config()),
            patch("socket.socket", return_value=StopOnConnectSocket()),
            patch.object(sys, "argv", ["relayinner-display-session"]),
            warnings.catch_warnings(),
            self.assertRaises(StopEntrypoint),
        ):
            warnings.filterwarnings(
                "ignore",
                message="'relayinner_display.session' found in sys.modules.*",
                category=RuntimeWarning,
            )
            runpy.run_module("relayinner_display.session", run_name="__main__")

        self.assertEqual(observed_paths, ["/run/relayinner-display/session.sock"])


def build_config(
    *,
    backend: str = "spice",
    vnc_bind_host: str = "127.0.0.1",
    vnc_display_number: int = 77,
    vnc_viewer: str = "remote-viewer",
    looking_glass_binary: str = "looking-glass-client",
    looking_glass_shm_file: Path = Path("/dev/kvmfr0"),
    looking_glass_renderer: str = "auto",
    looking_glass_fullscreen: bool = True,
    looking_glass_disable_host_screensaver: bool = True,
    looking_glass_spice_enabled: bool = True,
    moonlight_binary: str = "moonlight",
    moonlight_host: str = "192.168.50.20",
    moonlight_base_port: int = 47989,
    moonlight_app: str = "Desktop",
    moonlight_state_dir: Path = Path("/var/lib/relayinner-display/moonlight"),
    moonlight_resolution: str | None = None,
    moonlight_quit_app_after_session: bool = False,
    power_helper: str = "wlr-randr",
) -> AppConfig:
    run_dir = Path("/run/relayinner-display")
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
            shm_file=looking_glass_shm_file,
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
            state_dir=moonlight_state_dir,
            resolution=moonlight_resolution,
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
            output_name="HDMI-A-1",
            power_helper=power_helper,
        ),
        kiosk=KioskConfig(
            compositor="auto",
            resolved_compositor=resolve_kiosk_compositor(backend, "auto"),
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
