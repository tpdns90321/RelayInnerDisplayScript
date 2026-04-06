from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which
from typing import Any, Callable
import grp
import logging
import os
import pwd
import socket
import stat
import time

from .config import AppConfig, ConfigError, build_fallback_config, load_config
from .input import EvdevPowerButtonSource, LogindPowerButtonPolicyChecker, PowerButtonError
from .ipc import decode_message, encode_message, validate_session_message
from .models import RuntimeState, SessionState, write_runtime_state
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
}
IMPLEMENTED_CONSOLE_BACKENDS = {"spice", "vnc", "looking-glass"}
REQUIRED_DAEMON_BINARIES: tuple[str, ...] = ()


class RuntimeValidationError(RuntimeError):
    """Raised when runtime prerequisites are missing or inaccessible."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def subsystem_logger(namespace: str, subsystem: str) -> logging.Logger:
    return logging.getLogger(f"{namespace}.{subsystem}")


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
            vnc_endpoint=config.console.vnc.endpoint if config.console.vnc is not None else None,
            looking_glass_shm_file=(
                str(config.console.looking_glass.shm_file)
                if config.console.looking_glass is not None
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
        self.validate_runtime_dependencies = dependency_finder is not None or isinstance(
            proxmox,
            ProxmoxClient,
        )
        self.startup_error = startup_error
        self.last_power_button_accepted_at: datetime | None = None
        self.power_button_action_started_at: datetime | None = None
        self.proxmox_failure_timestamps: list[datetime] = []

    def prepare_runtime(self) -> None:
        self.config.runtime.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.runtime.run_dir, 0o750)
        chown_if_present(self.config.runtime.run_dir, SESSION_USER, SESSION_GROUP, self.logger)
        self.config.console.artifact_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.console.artifact_dir, 0o750)
        chown_if_present(self.config.console.artifact_dir, SESSION_USER, SESSION_GROUP, self.logger)

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
            "Daemon started for VM %s on node %s with backend=%s",
            self.state.vmid,
            self.state.node_name,
            self.config.target.console_backend,
        )

    def on_session_disconnected(self) -> None:
        self.session_ready = False
        self.console_running = False
        self.console_pid = None
        self.state.active_console_backend = None
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
            if not self.console_running and self.state.display_power_intent == "on":
                self._transition(self._waiting_session_state())
            self._persist_state()
            return actions + display_actions

        if self.console_running:
            self.state.last_error = None
            self._transition(SessionState.SHOWING_CONSOLE)
            self._persist_state()
            return actions + display_actions

        if self.next_reconnect_at is not None and timestamp < self.next_reconnect_at:
            self._transition(SessionState.RECONNECTING_CONSOLE)
            self._persist_state()
            return actions + display_actions

        backend = self.config.target.console_backend
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
        if self.config.target.console_backend != "looking-glass":
            return

        self._validate_looking_glass_preflight(
            check_binary=self.validate_runtime_dependencies,
            check_session_access=self.validate_runtime_dependencies,
        )
        looking_glass = self.config.console.looking_glass
        if looking_glass is None:
            raise AssertionError("Looking Glass backend selected without console.looking_glass config")
        self.console_logger.info(
            "Looking Glass startup preflight satisfied: shm_file=%s renderer=%s",
            looking_glass.shm_file,
            looking_glass.renderer,
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
