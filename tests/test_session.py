from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from relayinner_display.config import (
    AppConfig,
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
