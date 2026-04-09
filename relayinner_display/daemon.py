from __future__ import annotations

from argparse import ArgumentParser
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import re
from shutil import which
from typing import Any, Callable
import grp
import logging
import os
import pwd
import socket
import stat
import subprocess
import time

from .config import (
    AppConfig,
    ConfigError,
    DEFAULT_MOONLIGHT_APP,
    build_fallback_config,
    load_config,
)
from .input import EvdevPowerButtonSource, LogindPowerButtonPolicyChecker, PowerButtonError
from .ipc import decode_message, encode_message, validate_session_message
from .models import MoonlightPairState, RuntimeState, SessionState, write_runtime_state
from .proxmox import (
    ProxmoxClient,
    ProxmoxCommandError,
    VncConfigurationError,
    VncEndpointUnavailableError,
)


DEFAULT_CONFIG_PATH = Path("/etc/relayinner-display/config.toml")
SESSION_USER = "relayinner-display"
SESSION_GROUP = "relayinner-display"
DISPLAY_ON_VM_STATES = {"running", "paused", "suspended"}
DISPLAY_OFF_VM_STATES = {"stopped", "shutdown"}
POWER_BUTTON_RUNNING_VM_STATES = {"running", "paused"}
POWER_BUTTON_STOPPED_VM_STATES = {"stopped", "shutdown"}
PROXMOX_FAILURE_LIMIT = 5
PROXMOX_FAILURE_WINDOW = timedelta(minutes=2)
CONSOLE_LAUNCHERS = {
    "spice": "remote-viewer",
    "vnc": "remote-viewer",
    "looking-glass": "looking-glass-client",
    "moonlight": "moonlight",
}
IMPLEMENTED_CONSOLE_BACKENDS = {"spice", "vnc", "looking-glass", "moonlight"}
REQUIRED_DAEMON_BINARIES: tuple[str, ...] = ()
MIN_MOONLIGHT_VERSION = (6, 0, 0)
MOONLIGHT_PAIR_TIMEOUT = timedelta(seconds=300)
MOONLIGHT_HOST_PROBE_TIMEOUT_S = 1.0
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MOONLIGHT_SETTINGS_KEY_RE = re.compile(r"(?P<index>\d+)[/\\\\](?P<field>[A-Za-z0-9_]+)")
MOONLIGHT_SETTINGS_SECTIONS = ("hostsbackup", "hosts")
MOONLIGHT_HOST_ADDRESS_FIELDS = (
    ("manualaddress", "manualport"),
    ("localaddress", "localport"),
    ("remoteaddress", "remoteport"),
    ("ipv6address", "ipv6port"),
)


class RuntimeValidationError(RuntimeError):
    """Raised when runtime prerequisites are missing or inaccessible."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
TCPConnectProbe = Callable[[str, int, float], None]
PinGenerator = Callable[[], str]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def subsystem_logger(namespace: str, subsystem: str) -> logging.Logger:
    return logging.getLogger(f"{namespace}.{subsystem}")


def generate_moonlight_pin() -> str:
    return f"{random.SystemRandom().randrange(10000):04d}"


def parse_moonlight_app_list_csv(output: str) -> list[str]:
    rows = [
        [field.strip() for field in row]
        for row in csv.reader(output.splitlines())
        if any(field.strip() for field in row)
    ]
    if not rows:
        return []

    name_index = 0
    header = [field.casefold() for field in rows[0]]
    if "name" in header:
        name_index = header.index("name")
        rows = rows[1:]

    app_names: list[str] = []
    for row in rows:
        if name_index >= len(row):
            continue
        app_name = row[name_index].strip()
        if app_name:
            app_names.append(app_name)
    return app_names


class SessionSocketServer:
    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self.path = path
        self.logger = logger or logging.getLogger(__name__)
        self.server: socket.socket | None = None
        self.connection: socket.socket | None = None
        self.buffer = b""

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.server.setblocking(False)
        os.chmod(self.path, 0o600)
        chown_if_present(self.path, SESSION_USER, SESSION_GROUP, self.logger)

    def accept_pending(self) -> bool:
        if self.server is None or self.connection is not None:
            return False

        try:
            connection, _ = self.server.accept()
        except BlockingIOError:
            return False

        connection.setblocking(False)
        self.connection = connection
        self.buffer = b""
        self.logger.info("Session socket connected")
        return True

    def read_messages(self) -> tuple[list[dict[str, Any]], bool]:
        if self.connection is None:
            return [], False

        messages: list[dict[str, Any]] = []
        disconnected = False
        while True:
            try:
                chunk = self.connection.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                disconnected = True
                self.close_client()
                break

            if not chunk:
                disconnected = True
                self.close_client()
                break

            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                messages.append(decode_message(line))

        return messages, disconnected

    def send_message(self, message: dict[str, object]) -> bool:
        if self.connection is None:
            return False

        try:
            self.connection.sendall(encode_message(message))
        except OSError:
            self.close_client()
            return False
        return True

    def close_client(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
                self.buffer = b""

    def close(self) -> None:
        self.close_client()
        if self.server is not None:
            try:
                self.server.close()
            finally:
                self.server = None
        if self.path.exists():
            self.path.unlink()


class DisplayDaemon:
    def __init__(
        self,
        config: AppConfig,
        proxmox: ProxmoxClient,
        logger: logging.Logger | None = None,
        power_button_source: Any | None = None,
        host_policy_checker: Any | None = None,
        dependency_finder: Callable[[str], str | None] | None = None,
        command_runner: CommandRunner = subprocess.run,
        tcp_connect_probe: TCPConnectProbe | None = None,
        pin_generator: PinGenerator | None = None,
        startup_error: str | None = None,
    ) -> None:
        self.config = config
        self.proxmox = proxmox
        self.logger = logger or logging.getLogger(config.runtime.log_namespace)
        self.namespace = self.logger.name
        self.proxmox_logger = subsystem_logger(self.namespace, "proxmox")
        self.session_logger = subsystem_logger(self.namespace, "session")
        self.console_logger = subsystem_logger(self.namespace, "console")
        self.display_logger = subsystem_logger(self.namespace, "display")
        self.input_logger = subsystem_logger(self.namespace, "input")
        self.state = RuntimeState(
            vmid=config.target.vmid,
            node_name="",
            console_backend=config.target.console_backend,
            kiosk_compositor=config.kiosk.resolved_compositor,
            display_drm_compatibility=config.display.drm_compatibility,
            vnc_endpoint=config.console.vnc.endpoint if config.console.vnc is not None else None,
            looking_glass_shm_file=(
                str(config.console.looking_glass.shm_file)
                if config.console.looking_glass is not None
                else None
            ),
            moonlight_host=(
                config.console.moonlight.host if config.console.moonlight is not None else None
            ),
            moonlight_base_port=(
                config.console.moonlight.base_port
                if config.console.moonlight is not None
                else None
            ),
            moonlight_app=(
                config.console.moonlight.app if config.console.moonlight is not None else None
            ),
            moonlight_resolution=(
                config.console.moonlight.resolution
                if config.console.moonlight is not None
                else None
            ),
        )
        self.session_ready = False
        self.console_running = False
        self.console_pid: int | None = None
        self.next_reconnect_at: datetime | None = None
        self.current_reconnect_delay_ms = config.policy.reconnect_initial_ms
        self.started_at: datetime | None = None
        self._power_state_since_at: datetime | None = None
        self._startup_display_policy_pending = True
        self.power_button_source = power_button_source
        self.host_policy_checker = host_policy_checker
        self.dependency_finder = dependency_finder or which
        self.command_runner = command_runner
        self.tcp_connect_probe = tcp_connect_probe or self._probe_tcp_connectivity
        self.pin_generator = pin_generator or generate_moonlight_pin
        self.validate_runtime_dependencies = dependency_finder is not None or isinstance(
            proxmox,
            ProxmoxClient,
        )
        self.startup_error = startup_error
        self.last_power_button_accepted_at: datetime | None = None
        self.power_button_action_started_at: datetime | None = None
        self.proxmox_failure_timestamps: list[datetime] = []
        self.moonlight_pair_requested_at: datetime | None = None
        self.moonlight_pair_launch_pending = False

    def prepare_runtime(self) -> None:
        self.config.runtime.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.runtime.run_dir, 0o750)
        chown_if_present(self.config.runtime.run_dir, SESSION_USER, SESSION_GROUP, self.logger)
        session_runtime_dir = self._session_runtime_dir()
        session_runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(session_runtime_dir, 0o700)
        chown_if_present(session_runtime_dir, SESSION_USER, SESSION_GROUP, self.logger)
        self.config.console.artifact_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.console.artifact_dir, 0o750)
        chown_if_present(self.config.console.artifact_dir, SESSION_USER, SESSION_GROUP, self.logger)
        self._prepare_moonlight_workspace()

        for path in (self.config.runtime.control_socket, *self._configured_console_artifact_paths()):
            if path.exists():
                path.unlink()

    def start(self, now: datetime | None = None) -> None:
        self.started_at = now or utcnow()
        if self.startup_error is not None:
            self._set_startup_error(self.startup_error)
            self._persist_state()
            self.session_logger.error("Daemon started in degraded mode: %s", self.startup_error)
            return

        self.state.node_name = self.proxmox.resolve_node_name(self.config.target.node_name)
        try:
            if self.validate_runtime_dependencies:
                self._validate_runtime_dependencies()
            self._validate_console_startup_prerequisites()
            self._prepare_power_button_capture()
        except (PowerButtonError, RuntimeValidationError) as exc:
            self._set_startup_error(str(exc))
            self._persist_state()
            self.session_logger.error("Daemon started in degraded mode: %s", exc)
            return

        self._transition(SessionState.WAITING_FOR_SESSION)
        self._persist_state()
        self.session_logger.info(
            "Daemon started for VM %s on node %s with backend=%s configured_kiosk_compositor=%s kiosk_compositor=%s",
            self.state.vmid,
            self.state.node_name,
            self.config.target.console_backend,
            self.config.kiosk.compositor,
            self.config.kiosk.resolved_compositor,
        )

    def on_session_disconnected(self) -> None:
        self.session_ready = False
        self.console_running = False
        self.console_pid = None
        self.state.active_console_backend = None
        self.moonlight_pair_launch_pending = False
        if self.startup_error is not None or self.state.degraded_reason is not None:
            self._transition(SessionState.DEGRADED)
        else:
            self._transition(SessionState.WAITING_FOR_SESSION)
        self._persist_state()
        self.session_logger.warning("Session disconnected")

    def handle_session_message(
        self,
        message: dict[str, object],
        now: datetime | None = None,
    ) -> list[dict[str, object]]:
        timestamp = now or utcnow()
        payload = validate_session_message(message)
        message_type = payload["type"]

        if message_type == "session_ready":
            self.session_ready = True
            self.console_running = False
            self.console_pid = None
            self.state.active_console_backend = None
            self.moonlight_pair_launch_pending = False
            self.session_logger.info("Session ready")
            if self.startup_error is not None or self.state.degraded_reason is not None:
                self._transition(SessionState.DEGRADED)
                self._persist_state()
                return [{"type": "show_waiting", "reason": "degraded"}]

            responses: list[dict[str, object]] = []
            responses.extend(self._reapply_display_power())
            vm_status, failure_actions = self._refresh_vm_status(timestamp)
            if vm_status is None:
                if self.state.session_state is SessionState.DEGRADED:
                    return responses + failure_actions + [{"type": "show_waiting", "reason": "degraded"}]
                self._transition(self._waiting_session_state())
                self._persist_state()
                return responses + failure_actions + [{"type": "show_waiting", "reason": "vm_stopped"}]
            if self._vm_can_show_console(self.state.vm_power_state) and self._moonlight_pairing_pending():
                self._transition(SessionState.WAITING_FOR_PAIRING)
                self._persist_state()
                responses.append(self._moonlight_pair_waiting_message())
                return responses
            if self._vm_can_show_console(self.state.vm_power_state):
                waiting_reason = "reconnecting" if self.next_reconnect_at is not None else "connecting"
                self._transition(
                    SessionState.RECONNECTING_CONSOLE
                    if waiting_reason == "reconnecting"
                    else SessionState.REQUESTING_CONSOLE
                )
                self._persist_state()
                responses.append({"type": "show_waiting", "reason": waiting_reason})
                return responses

            self._transition(self._waiting_session_state())
            self._persist_state()
            responses.append({"type": "show_waiting", "reason": "vm_stopped"})
            return responses

        if message_type == "console_started":
            backend = str(payload.get("backend") or self.config.target.console_backend)
            self.console_running = True
            self.console_pid = int(payload["pid"])
            self.state.active_console_backend = backend
            self.next_reconnect_at = None
            self.current_reconnect_delay_ms = self.config.policy.reconnect_initial_ms
            self.state.last_error = None
            if (
                backend == "moonlight"
                and self.state.moonlight_pair_state is MoonlightPairState.PENDING_PIN_APPROVAL
            ):
                self._transition(SessionState.WAITING_FOR_PAIRING)
            else:
                self._transition(SessionState.SHOWING_CONSOLE)
            self._persist_state()
            self.console_logger.info(
                "Console started: backend=%s pid=%s",
                backend,
                self.console_pid,
            )
            return []

        if message_type == "display_power_applied":
            self.state.display_power_applied = str(payload["state"])
            if not self.console_running and not self._vm_can_show_console(self.state.vm_power_state):
                self._transition(self._waiting_session_state())
            self._persist_state()
            self.display_logger.info("Display power applied in session: %s", payload["state"])
            return []

        if message_type == "console_exited":
            backend = str(
                payload.get("backend")
                or self.state.active_console_backend
                or self.config.target.console_backend
            )
            self.console_running = False
            self.console_pid = None
            self.state.active_console_backend = None
            exit_code = int(payload["code"])
            exit_signal = int(payload["signal"])
            self.state.mark_console_exit(timestamp, exit_code, exit_signal, backend)
            if (
                backend == "moonlight"
                and self.state.moonlight_pair_state is MoonlightPairState.PENDING_PIN_APPROVAL
            ):
                self.next_reconnect_at = None
                self.state.last_error = None
                self._transition(SessionState.WAITING_FOR_PAIRING)
                self._persist_state()
                self.console_logger.info(
                    "Moonlight pairing UI exited: code=%s signal=%s",
                    exit_code,
                    exit_signal,
                )
                return [self._moonlight_pair_waiting_message()]
            self.state.last_error = (
                f"Console backend={backend} exited unexpectedly (code={exit_code}, signal={exit_signal})"
            )
            if self._vm_can_show_console(self.state.vm_power_state):
                self._schedule_reconnect(timestamp)
                self._transition(SessionState.RECONNECTING_CONSOLE)
                self._persist_state()
                self.console_logger.warning("%s", self.state.last_error)
                return [{"type": "show_waiting", "reason": "reconnecting"}]

            self._transition(self._waiting_session_state())
            self._persist_state()
            return [{"type": "show_waiting", "reason": "vm_stopped"}]

        if message_type == "session_error":
            self.console_running = False
            self.console_pid = None
            return self._enter_degraded(str(payload["reason"]), subsystem="session")

        raise AssertionError(f"Unhandled session message type: {message_type}")

    def tick(self, now: datetime | None = None) -> list[dict[str, object]]:
        timestamp = now or utcnow()
        actions = self._poll_power_button_source(timestamp)
        if self.startup_error is not None or self.state.degraded_reason is not None:
            return actions

        vm_status, failure_actions = self._refresh_vm_status(timestamp)
        if vm_status is None:
            return actions + failure_actions

        self._refresh_power_button_action(timestamp, vm_status)
        display_actions = self._update_display_power_intent(timestamp)
        if not self.session_ready:
            self._persist_state()
            return actions

        if self._vm_is_off(vm_status):
            self.next_reconnect_at = None
            self.current_reconnect_delay_ms = self.config.policy.reconnect_initial_ms
            self._clear_moonlight_pair_state()
            actions_for_session: list[dict[str, object]] = []
            if self.console_running:
                self.console_running = False
                self.console_pid = None
                self.state.active_console_backend = None
                actions_for_session.append({"type": "disconnect_console", "reason": "vm_not_running"})
            if self.state.session_state not in (
                SessionState.WAITING_FOR_VM,
                SessionState.DISPLAY_SLEEPING,
            ):
                actions_for_session.append({"type": "show_waiting", "reason": "vm_stopped"})
            self._transition(self._waiting_session_state())
            self._persist_state()
            return actions + actions_for_session + display_actions

        if not self._vm_can_show_console(vm_status):
            self._clear_moonlight_pair_state()
            if not self.console_running and self.state.display_power_intent == "on":
                self._transition(self._waiting_session_state())
            self._persist_state()
            return actions + display_actions

        pairing_console_ready_to_stream = False
        if self.console_running:
            if (
                self.state.active_console_backend == "moonlight"
                and self.state.moonlight_pair_state is MoonlightPairState.PENDING_PIN_APPROVAL
            ):
                try:
                    pairing_blocked, pairing_actions = self._handle_moonlight_pairing(timestamp)
                except RuntimeValidationError as exc:
                    return actions + display_actions + self._enter_degraded(
                        "Console preparation failed for backend=moonlight: "
                        f"{exc}",
                        subsystem="console",
                    )
                if pairing_blocked:
                    self._persist_state()
                    return actions + display_actions + pairing_actions
                pairing_console_ready_to_stream = True
            else:
                self.state.last_error = None
                self._transition(SessionState.SHOWING_CONSOLE)
                self._persist_state()
                return actions + display_actions

        if self.next_reconnect_at is not None and timestamp < self.next_reconnect_at:
            self._transition(SessionState.RECONNECTING_CONSOLE)
            self._persist_state()
            return actions + display_actions

        backend = self.config.target.console_backend
        if backend == "moonlight" and not pairing_console_ready_to_stream:
            try:
                pairing_blocked, pairing_actions = self._handle_moonlight_pairing(timestamp)
            except RuntimeValidationError as exc:
                return actions + display_actions + self._enter_degraded(
                    f"Console preparation failed for backend={backend}: {exc}",
                    subsystem="console",
                )
            if pairing_blocked:
                self._persist_state()
                return actions + display_actions + pairing_actions

        try:
            self._transition(SessionState.REQUESTING_CONSOLE)
            connect_message = self._prepare_console_launch()
        except VncEndpointUnavailableError as exc:
            self.state.last_error = str(exc)
            self._schedule_reconnect(timestamp)
            self._transition(SessionState.RECONNECTING_CONSOLE)
            self._persist_state()
            self.console_logger.warning("%s", exc)
            return actions + display_actions + [{"type": "show_waiting", "reason": "reconnecting"}]
        except (RuntimeValidationError, VncConfigurationError) as exc:
            return actions + display_actions + self._enter_degraded(
                f"Console preparation failed for backend={backend}: {exc}",
                subsystem="console",
            )
        except ProxmoxCommandError as exc:
            return actions + display_actions + self._record_proxmox_failure(timestamp, str(exc))

        self._clear_proxmox_failures()
        self.state.last_error = None
        self.state.mark_connect_attempt(timestamp)
        self.next_reconnect_at = None
        self._transition(SessionState.REQUESTING_CONSOLE)
        self._persist_state()
        self.console_logger.info(
            "Prepared console launch for VM %s with backend=%s",
            self.state.vmid,
            backend,
        )
        return actions + display_actions + [connect_message]

    def close(self) -> None:
        if self.power_button_source is not None:
            close = getattr(self.power_button_source, "close", None)
            if callable(close):
                close()

    def _prepare_power_button_capture(self) -> None:
        if not self.config.input.forward_power_button:
            return

        checker = self.host_policy_checker or LogindPowerButtonPolicyChecker()
        source = self.power_button_source or EvdevPowerButtonSource(
            self.config.input.power_button_event
        )
        checker.validate()
        source.open()
        self.host_policy_checker = checker
        self.power_button_source = source
        self.input_logger.info(
            "Power-button forwarding enabled from %s",
            self.config.input.power_button_event,
        )

    def _poll_power_button_source(self, timestamp: datetime) -> list[dict[str, object]]:
        if not self.config.input.forward_power_button:
            return []
        if self.startup_error is not None or self.power_button_source is None:
            return []

        try:
            press_count = int(self.power_button_source.poll_presses())
        except (OSError, PowerButtonError) as exc:
            self._set_startup_error(f"Power-button device read failed: {exc}")
            self._persist_state()
            self.input_logger.error("Power-button forwarding disabled after read failure: %s", exc)
            return []

        for _ in range(press_count):
            self._handle_power_button_press(timestamp)
        return []

    def _handle_power_button_press(self, timestamp: datetime) -> None:
        vm_status, _ = self._refresh_vm_status(timestamp)
        if vm_status is None:
            self.state.last_power_button_result = "status_failed"
            self._persist_state()
            self.input_logger.error(
                "Ignored power-button press because VM status could not be read: %s",
                self.state.last_error,
            )
            return

        self._refresh_power_button_action(timestamp, vm_status)
        if self.state.power_button_action_in_flight:
            self.state.last_power_button_result = "ignored_in_flight"
            self._persist_state()
            self.input_logger.info(
                "Ignored power-button press while action=%s remains in flight",
                self.state.last_power_button_action,
            )
            return

        if self._within_power_button_debounce(timestamp):
            self.state.last_power_button_result = "ignored_debounced"
            self._persist_state()
            self.input_logger.info(
                "Ignored power-button press within debounce window (%sms)",
                self.config.input.debounce_ms,
            )
            return

        if vm_status in POWER_BUTTON_STOPPED_VM_STATES:
            action = self.config.policy.power_button_action_when_stopped
            command = lambda: self.proxmox.start_vm(self.config.target.vmid)
        elif vm_status in POWER_BUTTON_RUNNING_VM_STATES:
            action = self.config.policy.power_button_action_when_running
            command = lambda: self.proxmox.shutdown_vm(
                self.config.target.vmid,
                self.config.policy.shutdown_timeout_s,
            )
        else:
            self.state.last_power_button_result = "ignored_non_actionable"
            self._persist_state()
            self.input_logger.info(
                "Ignored power-button press for non-actionable VM state=%s",
                vm_status,
            )
            return

        try:
            command()
        except ProxmoxCommandError as exc:
            self.last_power_button_accepted_at = timestamp
            self.state.mark_power_button_press(timestamp, action, "failed")
            self.state.last_error = str(exc)
            self._persist_state()
            self.proxmox_logger.error(
                "Power-button action failed: action=%s vmid=%s error=%s",
                action,
                self.state.vmid,
                exc,
            )
            return

        self._clear_proxmox_failures()
        self.last_power_button_accepted_at = timestamp
        self.power_button_action_started_at = timestamp
        self.state.mark_power_button_press(timestamp, action, "submitted")
        self.state.last_error = None
        self._persist_state()
        self.input_logger.info(
            "Accepted power-button press: action=%s vmid=%s vm_state=%s",
            action,
            self.state.vmid,
            vm_status,
        )

    def _refresh_power_button_action(self, timestamp: datetime, vm_status: str) -> None:
        if not self.state.power_button_action_in_flight:
            return

        action = self.state.last_power_button_action
        if action == "start":
            if vm_status in POWER_BUTTON_RUNNING_VM_STATES:
                self.state.power_button_action_in_flight = False
                self.state.last_power_button_result = "completed"
                self.power_button_action_started_at = None
                self.input_logger.info("Observed completion of in-flight power-button start action")
                return
            if (
                self.power_button_action_started_at is not None
                and self._elapsed_ms(self.power_button_action_started_at, timestamp)
                >= self.config.input.debounce_ms
                and vm_status in POWER_BUTTON_STOPPED_VM_STATES
            ):
                self.state.power_button_action_in_flight = False
                self.state.last_power_button_result = "stalled"
                self.power_button_action_started_at = None
                self.input_logger.warning(
                    "In-flight power-button start action stalled in state=%s",
                    vm_status,
                )
                return
            if (
                self.power_button_action_started_at is not None
                and self._elapsed_ms(self.power_button_action_started_at, timestamp)
                >= self.config.policy.shutdown_timeout_s * 1000
                and vm_status not in POWER_BUTTON_STOPPED_VM_STATES
            ):
                self.state.power_button_action_in_flight = False
                self.state.last_power_button_result = "timed_out"
                self.power_button_action_started_at = None
                self.input_logger.warning(
                    "In-flight power-button start action timed out in state=%s",
                    vm_status,
                )
                return

        if action == "shutdown":
            if vm_status in POWER_BUTTON_STOPPED_VM_STATES:
                self.state.power_button_action_in_flight = False
                self.state.last_power_button_result = "completed"
                self.power_button_action_started_at = None
                self.input_logger.info("Observed completion of in-flight power-button shutdown action")
                return
            if (
                self.power_button_action_started_at is not None
                and self._elapsed_ms(self.power_button_action_started_at, timestamp)
                >= self.config.policy.shutdown_timeout_s * 1000
                and vm_status not in POWER_BUTTON_STOPPED_VM_STATES
            ):
                self.state.power_button_action_in_flight = False
                self.state.last_power_button_result = "timed_out"
                self.power_button_action_started_at = None
                self.input_logger.warning(
                    "In-flight power-button shutdown action timed out in state=%s",
                    vm_status,
                )

    def _set_startup_error(self, reason: str) -> None:
        self.startup_error = reason
        self._clear_moonlight_pair_state()
        self.state.last_error = reason
        self.state.degraded_reason = reason
        self.state.power_button_action_in_flight = False
        self.power_button_action_started_at = None
        self._transition(SessionState.DEGRADED)
        self.close()

    def _within_power_button_debounce(self, timestamp: datetime) -> bool:
        if self.last_power_button_accepted_at is None:
            return False
        return (
            self._elapsed_ms(self.last_power_button_accepted_at, timestamp)
            < self.config.input.debounce_ms
        )

    def _elapsed_ms(self, start: datetime, end: datetime) -> int:
        return int((end - start).total_seconds() * 1000)

    def _enter_degraded(
        self,
        reason: str,
        *,
        subsystem: str = "session",
    ) -> list[dict[str, object]]:
        had_console = self.console_running or self.console_pid is not None
        self.console_running = False
        self.console_pid = None
        self.state.active_console_backend = None
        self.state.last_error = reason
        self.state.degraded_reason = reason
        self._transition(SessionState.DEGRADED)
        self._persist_state()
        subsystem_logger(self.namespace, subsystem).error("Entering degraded mode: %s", reason)
        actions: list[dict[str, object]] = []
        if had_console:
            actions.append({"type": "disconnect_console", "reason": "control_error"})
        actions.append({"type": "show_waiting", "reason": "degraded"})
        return actions

    def _schedule_reconnect(self, now: datetime) -> None:
        delay_ms = self.current_reconnect_delay_ms
        self.next_reconnect_at = now + timedelta(milliseconds=delay_ms)
        self.current_reconnect_delay_ms = min(
            self.current_reconnect_delay_ms * 2,
            self.config.policy.reconnect_max_ms,
        )

    def _record_vm_power_state(self, vm_status: str, now: datetime) -> None:
        if self.state.vm_power_state != vm_status or self._power_state_since_at is None:
            self._power_state_since_at = now
            self.state.mark_power_state_since(now)
        self.state.vm_power_state = vm_status

    def _configured_console_artifact_paths(self) -> tuple[Path, ...]:
        if self.config.target.console_backend == "spice":
            return (self._spice_vv_path(),)
        return ()

    def _prepare_moonlight_workspace(self) -> None:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            return

        moonlight.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(moonlight.state_dir, 0o700)
        chown_if_present(moonlight.state_dir, SESSION_USER, SESSION_GROUP, self.logger)

        moonlight.portable_marker_path.touch(exist_ok=True)
        os.chmod(moonlight.portable_marker_path, 0o600)
        chown_if_present(moonlight.portable_marker_path, SESSION_USER, SESSION_GROUP, self.logger)

    def _handle_moonlight_pairing(
        self,
        now: datetime,
    ) -> tuple[bool, list[dict[str, object]]]:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        if not self._moonlight_host_is_reachable(moonlight.host, moonlight.base_port):
            self._clear_moonlight_pair_state()
            self.state.last_error = (
                "Moonlight host is not reachable yet: "
                f"{moonlight.host_authority} (tcp/{moonlight.base_port})"
            )
            self._schedule_reconnect(now)
            self._transition(SessionState.RECONNECTING_CONSOLE)
            self.console_logger.warning("%s", self.state.last_error)
            return True, [{"type": "show_waiting", "reason": "reconnecting"}]

        self.next_reconnect_at = None
        if self._moonlight_workspace_host_is_paired():
            self._mark_moonlight_as_paired()
            return False, []
        if (
            self._moonlight_workspace_contains_paired_host()
            and self._moonlight_host_reuses_existing_pairing()
        ):
            self._normalize_moonlight_workspace_host()
            self.console_logger.info(
                "Recovered Moonlight paired state for configured host=%s via live probe after workspace host mismatch",
                moonlight.host_authority,
            )
            self._mark_moonlight_as_paired()
            return False, []

        should_issue_pair = (
            self.state.moonlight_pair_state is not MoonlightPairState.PENDING_PIN_APPROVAL
            or self.state.moonlight_pair_pin is None
            or self.moonlight_pair_requested_at is None
            or now - self.moonlight_pair_requested_at >= MOONLIGHT_PAIR_TIMEOUT
            or not self.moonlight_pair_launch_pending
        )

        if should_issue_pair:
            pin = self.pin_generator()
            self.state.moonlight_pair_pin = pin
            self.moonlight_pair_requested_at = now
            self.moonlight_pair_launch_pending = True

        self._set_moonlight_pair_state(MoonlightPairState.PENDING_PIN_APPROVAL)
        self.state.last_error = None
        previous_state = self.state.session_state
        self._transition(SessionState.WAITING_FOR_PAIRING)
        if should_issue_pair:
            return True, [self._prepare_moonlight_pair_launch(self.state.moonlight_pair_pin)]
        if previous_state is SessionState.WAITING_FOR_PAIRING:
            return True, []
        return True, [self._moonlight_pair_waiting_message()]

    def _moonlight_pairing_pending(self) -> bool:
        return (
            self.config.target.console_backend == "moonlight"
            and self.state.moonlight_pair_state is MoonlightPairState.PENDING_PIN_APPROVAL
            and self.state.moonlight_pair_pin is not None
        )

    def _moonlight_pair_waiting_message(self) -> dict[str, object]:
        moonlight = self.config.console.moonlight
        if moonlight is None or self.state.moonlight_pair_pin is None:
            raise AssertionError("Moonlight pairing wait requested without active Moonlight PIN")

        return {
            "type": "show_waiting",
            "reason": "pairing_required",
            "details": {
                "backend": "moonlight",
                "host": moonlight.host_authority,
                "pin": self.state.moonlight_pair_pin,
                "instructions": "Open the Sunshine web UI PIN page on the guest and enter this PIN.",
            },
        }

    def _set_moonlight_pair_state(self, state: MoonlightPairState) -> None:
        if self.state.moonlight_pair_state != state:
            self.console_logger.info(
                "Moonlight pair state transition: %s -> %s",
                self.state.moonlight_pair_state.value,
                state.value,
            )
        self.state.moonlight_pair_state = state

    def _mark_moonlight_as_paired(self) -> None:
        self._set_moonlight_pair_state(MoonlightPairState.PAIRED)
        self.state.moonlight_pair_pin = None
        self.moonlight_pair_requested_at = None
        self.moonlight_pair_launch_pending = False
        self.state.last_error = None

    def _clear_moonlight_pair_state(self) -> None:
        self.state.moonlight_pair_pin = None
        self.moonlight_pair_requested_at = None
        self.moonlight_pair_launch_pending = False
        self._set_moonlight_pair_state(MoonlightPairState.UNKNOWN)

    def _moonlight_host_is_reachable(self, host: str, port: int) -> bool:
        try:
            self.tcp_connect_probe(host, port, MOONLIGHT_HOST_PROBE_TIMEOUT_S)
        except OSError:
            return False
        return True

    def _moonlight_workspace_host_is_paired(self) -> bool:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        configured_host = self._normalize_moonlight_host_value(moonlight.host)
        for record in self._moonlight_workspace_host_records():
            if not self._moonlight_workspace_record_matches_host(
                record,
                configured_host,
                moonlight.base_port,
            ):
                continue
            if record.get("srvcert", "").strip():
                return True
        return False

    def _moonlight_workspace_host_records(self) -> list[dict[str, str]]:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        records_by_section: dict[str, dict[int, dict[str, str]]] = {
            section: {} for section in MOONLIGHT_SETTINGS_SECTIONS
        }
        for settings_path in sorted(moonlight.state_dir.rglob("*.ini")):
            if not settings_path.is_file():
                continue
            try:
                lines = settings_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            active_section: str | None = None
            for raw_line in lines:
                line = raw_line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    active_section = line[1:-1].strip().casefold()
                    continue
                if active_section not in records_by_section:
                    continue

                key, separator, value = line.partition("=")
                if not separator:
                    continue
                match = MOONLIGHT_SETTINGS_KEY_RE.fullmatch(key.strip())
                if match is None:
                    continue

                index = int(match.group("index"))
                field = match.group("field").casefold()
                records_by_section[active_section].setdefault(index, {})[field] = value.strip()

        for section in MOONLIGHT_SETTINGS_SECTIONS:
            section_records = records_by_section[section]
            if section_records:
                return [section_records[index] for index in sorted(section_records)]
        return []

    def _moonlight_workspace_contains_paired_host(self) -> bool:
        return any(record.get("srvcert", "").strip() for record in self._moonlight_workspace_host_records())

    def _normalize_moonlight_workspace_host(self) -> None:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        host_value = moonlight.host.strip()
        if host_value.startswith("[") and host_value.endswith("]"):
            host_value = host_value[1:-1]

        updated_paths: list[Path] = []
        for settings_path in sorted(moonlight.state_dir.rglob("*.ini")):
            if self._rewrite_moonlight_workspace_settings_file(
                settings_path,
                host=host_value,
                port=moonlight.base_port,
            ):
                updated_paths.append(settings_path)

        if updated_paths:
            joined_paths = ",".join(str(path) for path in updated_paths)
            self.console_logger.info(
                "Normalized Moonlight paired host records for configured host=%s in %s",
                moonlight.host_authority,
                joined_paths,
            )

    def _rewrite_moonlight_workspace_settings_file(
        self,
        settings_path: Path,
        *,
        host: str,
        port: int,
    ) -> bool:
        try:
            original_lines = settings_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError as exc:
            self.console_logger.warning(
                "Failed to read Moonlight settings file for host normalization: %s: %s",
                settings_path,
                exc,
            )
            return False

        relevant_sections = set(MOONLIGHT_SETTINGS_SECTIONS)
        section_end_indexes: dict[str, int] = {}
        records: dict[tuple[str, int], dict[str, object]] = {}
        active_section: str | None = None

        for index, raw_line in enumerate(original_lines):
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                active_section = line[1:-1].strip().casefold()
                if active_section in relevant_sections:
                    section_end_indexes.setdefault(active_section, index + 1)
                continue

            if active_section not in relevant_sections:
                continue

            section_end_indexes[active_section] = index + 1
            if not line or line.startswith(("#", ";")):
                continue

            key, separator, value = line.partition("=")
            if not separator:
                continue

            match = MOONLIGHT_SETTINGS_KEY_RE.fullmatch(key.strip())
            if match is None:
                continue

            record_index = int(match.group("index"))
            field = match.group("field").casefold()
            record = records.setdefault(
                (active_section, record_index),
                {"fields": {}, "paired": False},
            )
            record_fields = record["fields"]
            assert isinstance(record_fields, dict)
            record_fields[field] = index
            if field == "srvcert" and value.strip():
                record["paired"] = True

        replacement_lines = list(original_lines)
        insertions_by_section: dict[str, list[str]] = {}
        stale_fields = {field for field, _ in MOONLIGHT_HOST_ADDRESS_FIELDS[1:]}
        stale_fields.update({port_field for _, port_field in MOONLIGHT_HOST_ADDRESS_FIELDS[1:]})
        updated = False

        for record_key, record in records.items():
            paired = bool(record["paired"])
            if not paired:
                continue

            section, record_index = record_key
            record_fields = record["fields"]
            assert isinstance(record_fields, dict)

            desired_values = {
                "hostname": host,
                "manualaddress": host,
                "manualport": str(port),
            }
            for field, value in desired_values.items():
                field_line = record_fields.get(field)
                rendered = f"{record_index}\\{field}={value}"
                if isinstance(field_line, int):
                    if replacement_lines[field_line] != rendered:
                        replacement_lines[field_line] = rendered
                        updated = True
                else:
                    insertions_by_section.setdefault(section, []).append(rendered)
                    updated = True

            for field in stale_fields:
                field_line = record_fields.get(field)
                if not isinstance(field_line, int):
                    continue
                rendered = f"{record_index}\\{field}="
                if replacement_lines[field_line] != rendered:
                    replacement_lines[field_line] = rendered
                    updated = True

        if not updated:
            return False

        for section, insert_at in sorted(
            section_end_indexes.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            new_lines = insertions_by_section.get(section)
            if not new_lines:
                continue
            replacement_lines[insert_at:insert_at] = new_lines

        try:
            mode = settings_path.stat().st_mode & 0o777
        except OSError:
            mode = None

        try:
            settings_path.write_text(
                "\n".join(replacement_lines) + "\n",
                encoding="utf-8",
            )
            if mode is not None:
                os.chmod(settings_path, mode)
            chown_if_present(settings_path, SESSION_USER, SESSION_GROUP, self.logger)
        except OSError as exc:
            self.console_logger.warning(
                "Failed to update Moonlight settings file for host normalization: %s: %s",
                settings_path,
                exc,
            )
            return False

        return True

    def _moonlight_workspace_record_matches_host(
        self,
        record: dict[str, str],
        configured_host: str,
        configured_port: int,
    ) -> bool:
        hostname = self._normalize_moonlight_host_value(record.get("hostname", ""))
        if hostname and hostname == configured_host:
            return True

        for address_field, port_field in MOONLIGHT_HOST_ADDRESS_FIELDS:
            address = self._normalize_moonlight_host_value(record.get(address_field, ""))
            if not address or address != configured_host:
                continue

            configured_record_port = record.get(port_field, "").strip()
            if not configured_record_port:
                return True
            try:
                return int(configured_record_port) == configured_port
            except ValueError:
                return False
        return False

    def _normalize_moonlight_host_value(self, value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("[") and normalized.endswith("]"):
            normalized = normalized[1:-1]
        return normalized.casefold()

    def _moonlight_app_is_available(self, available_apps: list[str]) -> bool:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        configured_app = moonlight.app.strip().casefold()
        return any(app.strip().casefold() == configured_app for app in available_apps)

    def _moonlight_app_uses_desktop_stream(self) -> bool:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        return moonlight.app.strip().casefold() == DEFAULT_MOONLIGHT_APP.casefold()

    def _moonlight_host_reuses_existing_pairing(self) -> bool:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        try:
            self._moonlight_list_apps()
        except RuntimeValidationError as exc:
            self.console_logger.info(
                "Moonlight live probe did not confirm existing pairing for host=%s: %s",
                moonlight.host_authority,
                exc,
            )
            return False
        return True

    def _moonlight_list_apps(self) -> list[str]:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        completed = self._run_moonlight_command(["list", moonlight.host_authority])
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if detail:
                raise RuntimeValidationError(
                    "Moonlight app-list retrieval failed for "
                    f"{moonlight.host_authority}: {detail}"
                )
            raise RuntimeValidationError(
                "Moonlight app-list retrieval failed for "
                f"{moonlight.host_authority} with exit code {completed.returncode}"
            )
        return parse_moonlight_app_list_csv(completed.stdout)

    def _prepare_moonlight_pair_launch(self, pin: str | None) -> dict[str, object]:
        moonlight = self.config.console.moonlight
        if moonlight is None or pin is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        self.console_logger.info(
            "Launching Moonlight pairing UI for host=%s workspace=%s",
            moonlight.host_authority,
            moonlight.state_dir,
        )
        return {
            "type": "connect_console",
            "backend": "moonlight",
            "launcher": moonlight.binary,
            "cwd": str(moonlight.state_dir),
            "argv": [moonlight.binary, "pair", moonlight.host_authority, "--pin", pin],
        }

    def _run_moonlight_command(self, subcommand: list[str]) -> subprocess.CompletedProcess[str]:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        command = [self._resolved_binary(moonlight.binary), *subcommand]
        if subcommand[:1] == ["list"]:
            command.append("--csv")
        try:
            return self.command_runner(
                command,
                cwd=str(moonlight.state_dir),
                env=self._moonlight_helper_env(),
                text=True,
                capture_output=True,
                check=False,
                timeout=self.config.policy.command_timeout_s,
                **self._session_user_command_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeValidationError(
                "Moonlight command timed out after "
                f"{self.config.policy.command_timeout_s}s: {' '.join(command)}"
            ) from exc
        except OSError as exc:
            raise RuntimeValidationError(
                f"Failed to run Moonlight command {' '.join(subcommand)}: {exc}"
            ) from exc

    def _session_user_command_kwargs(self) -> dict[str, object]:
        if os.geteuid() != 0:
            return {}

        try:
            session_entry = pwd.getpwnam(SESSION_USER)
        except KeyError as exc:
            raise RuntimeValidationError(f"Session user does not exist: {SESSION_USER}") from exc

        extra_groups = sorted(
            self._session_user_group_ids(session_entry.pw_name, session_entry.pw_gid)
            - {session_entry.pw_gid}
        )
        return {
            "user": session_entry.pw_uid,
            "group": session_entry.pw_gid,
            "extra_groups": extra_groups,
        }

    def _moonlight_helper_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PATH", os.defpath)
        env["XDG_SESSION_TYPE"] = "wayland"
        # Moonlight's CLI helpers still construct QGuiApplication even for
        # non-interactive commands like `list` and `--version`. Force a
        # headless Qt platform here so daemon-side checks don't fall back to
        # EGLFS/DRM when they run outside the kiosk's Wayland session.
        env["QT_QPA_PLATFORM"] = "offscreen"

        if os.geteuid() != 0:
            env.setdefault("XDG_RUNTIME_DIR", str(self._session_runtime_dir()))
            return env

        try:
            session_entry = pwd.getpwnam(SESSION_USER)
        except KeyError as exc:
            raise RuntimeValidationError(f"Session user does not exist: {SESSION_USER}") from exc

        env["HOME"] = session_entry.pw_dir
        env["LOGNAME"] = session_entry.pw_name
        env["USER"] = session_entry.pw_name
        env["XDG_RUNTIME_DIR"] = str(self._session_runtime_dir())
        return env

    def _session_runtime_dir(self) -> Path:
        return self.config.runtime.run_dir / "user-runtime"

    def _probe_tcp_connectivity(self, host: str, port: int, timeout_s: float) -> None:
        connection = socket.create_connection((host, port), timeout=timeout_s)
        connection.close()

    def _spice_vv_path(self) -> Path:
        if self.config.console.spice is not None:
            return self.config.console.spice.vv_path
        return self.config.runtime.spice_vv_path

    def _prepare_console_launch(self) -> dict[str, object]:
        backend = self.config.target.console_backend
        if backend == "spice":
            spice_vv_path = self._spice_vv_path()
            spice_config = self.proxmox.request_spice_config(
                self.state.node_name,
                self.config.target.vmid,
            )
            self.proxmox.write_vv_file(spice_vv_path, spice_config)
            os.chmod(spice_vv_path, 0o640)
            chown_if_present(
                spice_vv_path,
                SESSION_USER,
                SESSION_GROUP,
                self.logger,
            )
            return {
                "type": "connect_console",
                "backend": backend,
                "launcher": CONSOLE_LAUNCHERS[backend],
                "argv": [CONSOLE_LAUNCHERS[backend], "--full-screen", str(spice_vv_path)],
            }

        if backend == "vnc":
            vnc = self.config.console.vnc
            if vnc is None:
                raise AssertionError("VNC backend selected without console.vnc config")

            self.proxmox.validate_vnc_configuration(
                self.config.target.vmid,
                bind_host=vnc.bind_host,
                display_number=vnc.display_number,
            )
            self.proxmox.probe_vnc_endpoint(vnc.bind_host, vnc.port)
            self.state.vnc_endpoint = vnc.endpoint
            self.console_logger.info(
                "Validated VNC endpoint for VM %s at %s",
                self.state.vmid,
                self.state.vnc_endpoint,
            )
            return {
                "type": "connect_console",
                "backend": backend,
                "launcher": vnc.viewer,
                "argv": [vnc.viewer, "--full-screen", vnc.uri],
            }

        if backend == "looking-glass":
            looking_glass = self.config.console.looking_glass
            if looking_glass is None:
                raise AssertionError(
                    "Looking Glass backend selected without console.looking_glass config"
                )

            self._validate_looking_glass_preflight(
                check_binary=self.validate_runtime_dependencies,
                check_session_access=self.validate_runtime_dependencies,
            )
            self.state.looking_glass_shm_file = str(looking_glass.shm_file)
            self.console_logger.info(
                "Validated Looking Glass shared memory for VM %s at %s with renderer=%s",
                self.state.vmid,
                self.state.looking_glass_shm_file,
                looking_glass.renderer,
            )
            return {
                "type": "connect_console",
                "backend": backend,
                "launcher": looking_glass.binary,
                "argv": looking_glass.argv,
            }

        if backend == "moonlight":
            moonlight = self.config.console.moonlight
            if moonlight is None:
                raise AssertionError("Moonlight backend selected without console.moonlight config")

            if self._moonlight_app_uses_desktop_stream():
                self.console_logger.info(
                    "Skipping Moonlight app-list validation for Desktop stream on host=%s; "
                    "paired workspace state already exists",
                    moonlight.host_authority,
                )
            else:
                available_apps = self._moonlight_list_apps()
                if not self._moonlight_app_is_available(available_apps):
                    raise RuntimeValidationError(
                        "Configured Moonlight app is not available on "
                        f"{moonlight.host_authority}: {moonlight.app}"
                    )
                self.console_logger.info(
                    "Validated Moonlight app=%s from live app list on host=%s",
                    moonlight.app,
                    moonlight.host_authority,
                )

            if moonlight.resolution is not None:
                self.console_logger.info(
                    "Applying Moonlight resolution override for host=%s: %s",
                    moonlight.host_authority,
                    moonlight.resolution,
                )

            self.console_logger.info(
                "Prepared Moonlight launch for VM %s with backend=moonlight app=%s host=%s workspace=%s",
                self.state.vmid,
                moonlight.app,
                moonlight.host_authority,
                moonlight.state_dir,
            )
            return {
                "type": "connect_console",
                "backend": backend,
                "launcher": moonlight.binary,
                "cwd": str(moonlight.state_dir),
                "argv": moonlight.argv,
            }

        raise AssertionError(f"Unhandled console backend: {backend}")

    def _validate_runtime_dependencies(self) -> None:
        required = [*REQUIRED_DAEMON_BINARIES, self.config.display.power_helper]
        if self.config.target.console_backend in IMPLEMENTED_CONSOLE_BACKENDS:
            required.append(self._configured_console_launcher())
        seen: set[str] = set()
        for binary in required:
            key = str(binary)
            if key in seen:
                continue
            seen.add(key)
            self._ensure_binary_available(binary)

    def _validate_console_startup_prerequisites(self) -> None:
        backend = self.config.target.console_backend
        if backend == "looking-glass":
            self._validate_looking_glass_preflight(
                check_binary=self.validate_runtime_dependencies,
                check_session_access=self.validate_runtime_dependencies,
            )
            looking_glass = self.config.console.looking_glass
            if looking_glass is None:
                raise AssertionError(
                    "Looking Glass backend selected without console.looking_glass config"
                )
            self.console_logger.info(
                "Looking Glass startup preflight satisfied: shm_file=%s renderer=%s",
                looking_glass.shm_file,
                looking_glass.renderer,
            )
            return

        if backend != "moonlight":
            return

        self._validate_moonlight_startup_prerequisites(
            check_binary=self.validate_runtime_dependencies,
        )
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")
        self.console_logger.info(
            "Moonlight startup contract satisfied: host=%s workspace=%s binary=%s",
            moonlight.host_authority,
            moonlight.state_dir,
            moonlight.binary,
        )

    def _configured_console_launcher(self) -> str:
        backend = self.config.target.console_backend
        if backend == "looking-glass":
            looking_glass = self.config.console.looking_glass
            if looking_glass is None:
                raise AssertionError(
                    "Looking Glass backend selected without console.looking_glass config"
                )
            return looking_glass.binary
        if backend == "moonlight":
            moonlight = self.config.console.moonlight
            if moonlight is None:
                raise AssertionError("Moonlight backend selected without console.moonlight config")
            return moonlight.binary
        return CONSOLE_LAUNCHERS[backend]

    def _ensure_binary_available(self, binary: str) -> None:
        binary_path = Path(binary)
        if binary_path.is_absolute():
            if not binary_path.exists():
                raise RuntimeValidationError(f"Missing required binary: {binary}")
            if not os.access(binary_path, os.X_OK):
                raise RuntimeValidationError(f"Configured binary is not executable: {binary}")
            return

        if self.dependency_finder(binary) is None:
            raise RuntimeValidationError(f"Missing required binary: {binary}")

    def _validate_looking_glass_preflight(
        self,
        *,
        check_binary: bool,
        check_session_access: bool,
    ) -> None:
        looking_glass = self.config.console.looking_glass
        if looking_glass is None:
            raise AssertionError("Looking Glass backend selected without console.looking_glass config")

        if check_binary:
            self._ensure_binary_available(looking_glass.binary)

        shm_file = looking_glass.shm_file
        if not shm_file.exists():
            raise RuntimeValidationError(
                f"Looking Glass shared memory path does not exist: {shm_file}"
            )
        if shm_file.is_dir():
            raise RuntimeValidationError(
                f"Looking Glass shared memory path is not a file or device: {shm_file}"
            )
        if not check_session_access:
            return

        if not self._session_user_can_read_path(shm_file):
            raise RuntimeValidationError(
                "Looking Glass shared memory path is not readable by session user "
                f"{SESSION_USER}: {shm_file}"
            )

        flags = os.O_RDONLY
        non_blocking = getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(shm_file, flags | non_blocking)
        except OSError as exc:
            raise RuntimeValidationError(
                f"Looking Glass shared memory path could not be opened: {shm_file}: {exc}"
            ) from exc
        os.close(descriptor)

    def _validate_moonlight_startup_prerequisites(
        self,
        *,
        check_binary: bool,
    ) -> None:
        moonlight = self.config.console.moonlight
        if moonlight is None:
            raise AssertionError("Moonlight backend selected without console.moonlight config")

        if check_binary:
            self._ensure_binary_available(moonlight.binary)
            version = self._moonlight_version(moonlight.binary)
            if version < MIN_MOONLIGHT_VERSION:
                actual = ".".join(str(part) for part in version)
                minimum = ".".join(str(part) for part in MIN_MOONLIGHT_VERSION)
                raise RuntimeValidationError(
                    f"Moonlight version must be >= {minimum}, found {actual}"
                )

        if not moonlight.state_dir.is_dir():
            raise RuntimeValidationError(
                f"Moonlight state_dir is not a directory: {moonlight.state_dir}"
            )
        if not moonlight.portable_marker_path.exists():
            raise RuntimeValidationError(
                f"Moonlight portable marker is missing: {moonlight.portable_marker_path}"
            )

    def _moonlight_version(self, binary: str) -> tuple[int, int, int]:
        command = [self._resolved_binary(binary), "--version"]
        try:
            completed = self.command_runner(
                command,
                env=self._moonlight_helper_env(),
                text=True,
                capture_output=True,
                check=False,
                timeout=self.config.policy.command_timeout_s,
                **self._session_user_command_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeValidationError(
                "Moonlight version check timed out after "
                f"{self.config.policy.command_timeout_s}s: {binary}"
            ) from exc
        except OSError as exc:
            raise RuntimeValidationError(
                f"Failed to read Moonlight version from {binary}: {exc}"
            ) from exc

        output = "\n".join(
            part for part in ((completed.stdout or "").strip(), (completed.stderr or "").strip()) if part
        )
        match = VERSION_RE.search(output)
        if completed.returncode != 0 or match is None:
            detail = output or f"exit status {completed.returncode}"
            raise RuntimeValidationError(
                f"Unable to determine Moonlight version from {binary}: {detail}"
            )

        return tuple(int(match.group(index)) for index in range(1, 4))

    def _resolved_binary(self, binary: str) -> str:
        binary_path = Path(binary)
        if binary_path.is_absolute():
            return str(binary_path)
        resolved = self.dependency_finder(binary)
        return resolved or binary

    def _session_user_can_read_path(self, path: Path) -> bool:
        try:
            session_entry = pwd.getpwnam(SESSION_USER)
        except KeyError as exc:
            raise RuntimeValidationError(f"Session user does not exist: {SESSION_USER}") from exc

        stat_result = path.stat()
        mode = stat_result.st_mode
        if stat_result.st_uid == session_entry.pw_uid:
            return bool(mode & stat.S_IRUSR)

        group_ids = self._session_user_group_ids(session_entry.pw_name, session_entry.pw_gid)
        if stat_result.st_gid in group_ids:
            return bool(mode & stat.S_IRGRP)

        return bool(mode & stat.S_IROTH)

    def _session_user_group_ids(self, username: str, primary_gid: int) -> set[int]:
        group_ids = {primary_gid}
        for group in grp.getgrall():
            if username in group.gr_mem:
                group_ids.add(group.gr_gid)
        return group_ids

    def _refresh_vm_status(self, now: datetime) -> tuple[str | None, list[dict[str, object]]]:
        try:
            vm_status = self.proxmox.get_vm_status(self.config.target.vmid)
        except ProxmoxCommandError as exc:
            return None, self._record_proxmox_failure(now, str(exc))

        self._clear_proxmox_failures()
        self._record_vm_power_state(vm_status, now)
        return vm_status, []

    def _record_proxmox_failure(self, now: datetime, reason: str) -> list[dict[str, object]]:
        self.proxmox_failure_timestamps = [
            timestamp
            for timestamp in self.proxmox_failure_timestamps
            if now - timestamp <= PROXMOX_FAILURE_WINDOW
        ]
        self.proxmox_failure_timestamps.append(now)
        self.state.last_error = reason
        self.proxmox_logger.warning(
            "Command failure %s/%s within %ss: %s",
            len(self.proxmox_failure_timestamps),
            PROXMOX_FAILURE_LIMIT,
            int(PROXMOX_FAILURE_WINDOW.total_seconds()),
            reason,
        )
        if len(self.proxmox_failure_timestamps) >= PROXMOX_FAILURE_LIMIT:
            return self._enter_degraded(reason, subsystem="proxmox")
        self._persist_state()
        return []

    def _clear_proxmox_failures(self) -> None:
        self.proxmox_failure_timestamps.clear()

    def _update_display_power_intent(self, now: datetime) -> list[dict[str, object]]:
        target_intent = self._desired_display_power_intent(now)
        if target_intent is None or target_intent == self.state.display_power_intent:
            return []

        previous_intent = self.state.display_power_intent
        self.state.display_power_intent = target_intent
        self.display_logger.info(
            "Display power intent changed: %s -> %s (vm_state=%s)",
            previous_intent,
            target_intent,
            self.state.vm_power_state,
        )
        if not self.session_ready:
            return []
        return [self._display_power_message(target_intent)]

    def _desired_display_power_intent(self, now: datetime) -> str | None:
        if (
            self.state.power_button_action_in_flight
            and self.state.last_power_button_action == "start"
        ):
            return "on"

        if self.state.vm_power_state in DISPLAY_ON_VM_STATES:
            self._startup_display_policy_pending = False
            return "on"

        if self.state.vm_power_state not in DISPLAY_OFF_VM_STATES:
            return None

        off_since = self._power_state_since_at
        if off_since is None:
            return None

        if self._startup_display_policy_pending:
            if self.started_at is None:
                return None
            stabilized_at = self.started_at + timedelta(
                milliseconds=self.config.policy.power_state_stabilize_ms
            )
            if off_since < stabilized_at:
                off_since = stabilized_at

        due_at = off_since + timedelta(milliseconds=self.config.policy.dpms_off_delay_ms)
        if now < due_at:
            return None

        self._startup_display_policy_pending = False
        return "off"

    def _display_power_message(self, state: str) -> dict[str, object]:
        return {
            "type": "display_power",
            "state": state,
            "output": self.config.display.output_name,
        }

    def _reapply_display_power(self) -> list[dict[str, object]]:
        return [self._display_power_message(self.state.display_power_intent)]

    def _waiting_session_state(self) -> SessionState:
        if (
            self.state.display_power_intent == "off"
            and self.state.display_power_applied == "off"
        ):
            return SessionState.DISPLAY_SLEEPING
        return SessionState.WAITING_FOR_VM

    def _vm_can_show_console(self, vm_status: str) -> bool:
        return vm_status in DISPLAY_ON_VM_STATES

    def _vm_is_off(self, vm_status: str) -> bool:
        return vm_status in DISPLAY_OFF_VM_STATES

    def _transition(self, session_state: SessionState) -> None:
        if self.state.session_state != session_state:
            self.session_logger.info(
                "State transition: %s -> %s",
                self.state.session_state.public_value(),
                session_state.public_value(),
            )
        self.state.session_state = session_state
        if session_state is not SessionState.DEGRADED:
            self.state.degraded_reason = None

    def _persist_state(self) -> None:
        self.state.session_ready = self.session_ready
        write_runtime_state(self.config.runtime.daemon_state_path, self.state)


def chown_if_present(path: Path, user: str, group: str, logger: logging.Logger) -> None:
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        logger.debug("Skipped ownership update for %s; %s:%s not present", path, user, group)
        return

    try:
        os.chown(path, uid, gid)
    except PermissionError:
        logger.debug("Skipped ownership update for %s due to permission error", path)


def configure_logging(namespace: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger(namespace)


def run(config_path: Path) -> int:
    startup_error: str | None = None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        config = build_fallback_config()
        startup_error = f"Invalid config: {exc}"

    logger = configure_logging(config.runtime.log_namespace)
    daemon = DisplayDaemon(
        config=config,
        proxmox=ProxmoxClient(timeout_s=config.policy.command_timeout_s),
        logger=logger,
        startup_error=startup_error,
    )
    socket_server = SessionSocketServer(config.runtime.control_socket, logger)

    daemon.prepare_runtime()
    daemon.start()
    socket_server.start()

    try:
        poll_interval_s = config.policy.poll_interval_ms / 1000
        while True:
            socket_server.accept_pending()
            messages, disconnected = socket_server.read_messages()
            if disconnected:
                daemon.on_session_disconnected()

            send_failed = False
            for message in messages:
                for response in daemon.handle_session_message(message):
                    if not socket_server.send_message(response):
                        daemon.on_session_disconnected()
                        send_failed = True
                        break
                if send_failed:
                    break

            if send_failed:
                time.sleep(poll_interval_s)
                continue

            for response in daemon.tick():
                if not socket_server.send_message(response):
                    daemon.on_session_disconnected()
                    break

            time.sleep(poll_interval_s)
    finally:
        daemon.close()
        socket_server.close()


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="relayinner-displayd")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
