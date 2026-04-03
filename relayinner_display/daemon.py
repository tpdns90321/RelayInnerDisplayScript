from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import grp
import logging
import os
import pwd
import socket
import time

from .config import AppConfig, load_config
from .ipc import decode_message, encode_message, validate_session_message
from .models import RuntimeState, SessionState, write_runtime_state
from .proxmox import ProxmoxClient, ProxmoxCommandError


DEFAULT_CONFIG_PATH = Path("/etc/relayinner-display/config.toml")
SESSION_USER = "relayinner-display"
SESSION_GROUP = "relayinner-display"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    ) -> None:
        self.config = config
        self.proxmox = proxmox
        self.logger = logger or logging.getLogger(config.runtime.log_namespace)
        self.state = RuntimeState(vmid=config.target.vmid, node_name="")
        self.session_ready = False
        self.console_running = False
        self.console_pid: int | None = None
        self.next_reconnect_at: datetime | None = None
        self.current_reconnect_delay_ms = config.policy.reconnect_initial_ms

    def prepare_runtime(self) -> None:
        self.config.runtime.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.runtime.run_dir, 0o750)
        chown_if_present(self.config.runtime.run_dir, SESSION_USER, SESSION_GROUP, self.logger)

        for path in (
            self.config.runtime.control_socket,
            self.config.runtime.spice_vv_path,
            self.config.runtime.daemon_state_path,
        ):
            if path.exists():
                path.unlink()

    def start(self, now: datetime | None = None) -> None:
        self.state.node_name = self.proxmox.resolve_node_name(self.config.target.node_name)
        self._transition(SessionState.WAITING_FOR_SESSION)
        self._persist_state()
        self.logger.info("Daemon started for VM %s on node %s", self.state.vmid, self.state.node_name)

    def on_session_disconnected(self) -> None:
        self.session_ready = False
        self.console_running = False
        self.console_pid = None
        self._transition(SessionState.WAITING_FOR_SESSION)
        self._persist_state()
        self.logger.warning("Session disconnected")

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
            self.logger.info("Session ready")
            if self.state.vm_power_state == "running":
                self._transition(SessionState.RECONNECTING_CONSOLE)
                self._persist_state()
                return [{"type": "show_waiting", "reason": "reconnecting"}]

            self._transition(SessionState.WAITING_FOR_VM)
            self._persist_state()
            return [{"type": "show_waiting", "reason": "vm_stopped"}]

        if message_type == "console_started":
            self.console_running = True
            self.console_pid = int(payload["pid"])
            self.next_reconnect_at = None
            self.current_reconnect_delay_ms = self.config.policy.reconnect_initial_ms
            self.state.last_error = None
            self._transition(SessionState.SHOWING_CONSOLE)
            self._persist_state()
            self.logger.info("Console started with pid=%s", self.console_pid)
            return []

        if message_type == "console_exited":
            self.console_running = False
            self.console_pid = None
            exit_code = int(payload["code"])
            exit_signal = int(payload["signal"])
            self.state.last_error = (
                f"remote-viewer exited unexpectedly (code={exit_code}, signal={exit_signal})"
            )
            if self.state.vm_power_state == "running":
                self._schedule_reconnect(timestamp)
                self._transition(SessionState.RECONNECTING_CONSOLE)
                self._persist_state()
                self.logger.warning("%s", self.state.last_error)
                return [{"type": "show_waiting", "reason": "reconnecting"}]

            self._transition(SessionState.WAITING_FOR_VM)
            self._persist_state()
            return [{"type": "show_waiting", "reason": "vm_stopped"}]

        if message_type == "session_error":
            self.console_running = False
            self.console_pid = None
            self.state.last_error = str(payload["reason"])
            self._transition(SessionState.DEGRADED)
            self._persist_state()
            self.logger.error("Session error: %s", self.state.last_error)
            return [{"type": "show_waiting", "reason": "degraded"}]

        raise AssertionError(f"Unhandled session message type: {message_type}")

    def tick(self, now: datetime | None = None) -> list[dict[str, object]]:
        timestamp = now or utcnow()
        if not self.session_ready:
            return []

        try:
            vm_status = self.proxmox.get_vm_status(self.config.target.vmid)
        except ProxmoxCommandError as exc:
            return self._enter_degraded(str(exc))

        self.state.vm_power_state = vm_status
        if vm_status != "running":
            self.next_reconnect_at = None
            self.current_reconnect_delay_ms = self.config.policy.reconnect_initial_ms
            self.state.last_error = None
            actions: list[dict[str, object]] = []
            if self.console_running:
                self.console_running = False
                self.console_pid = None
                actions.append({"type": "disconnect_console", "reason": "vm_not_running"})
            if self.state.session_state != SessionState.WAITING_FOR_VM:
                actions.append({"type": "show_waiting", "reason": "vm_stopped"})
            self._transition(SessionState.WAITING_FOR_VM)
            self._persist_state()
            return actions

        if self.console_running:
            self.state.last_error = None
            self._transition(SessionState.SHOWING_CONSOLE)
            self._persist_state()
            return []

        if self.next_reconnect_at is not None and timestamp < self.next_reconnect_at:
            self._transition(SessionState.RECONNECTING_CONSOLE)
            self._persist_state()
            return []

        try:
            self._transition(SessionState.REQUESTING_CONSOLE)
            spice_config = self.proxmox.request_spice_config(
                self.state.node_name,
                self.config.target.vmid,
            )
            self.proxmox.write_vv_file(self.config.runtime.spice_vv_path, spice_config)
            os.chmod(self.config.runtime.spice_vv_path, 0o640)
            chown_if_present(
                self.config.runtime.spice_vv_path,
                SESSION_USER,
                SESSION_GROUP,
                self.logger,
            )
        except ProxmoxCommandError as exc:
            return self._enter_degraded(str(exc))

        self.state.last_error = None
        self.state.mark_connect_attempt(timestamp)
        self.next_reconnect_at = None
        self._transition(SessionState.CONNECTING_CONSOLE)
        self._persist_state()
        self.logger.info("Prepared SPICE config for VM %s", self.state.vmid)
        return [{"type": "connect_spice", "vv_path": str(self.config.runtime.spice_vv_path)}]

    def _enter_degraded(self, reason: str) -> list[dict[str, object]]:
        had_console = self.console_running or self.console_pid is not None
        self.console_running = False
        self.console_pid = None
        self.state.last_error = reason
        self._transition(SessionState.DEGRADED)
        self._persist_state()
        self.logger.error("Entering degraded mode: %s", reason)
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

    def _transition(self, session_state: SessionState) -> None:
        if self.state.session_state != session_state:
            self.logger.info(
                "State transition: %s -> %s",
                self.state.session_state.value,
                session_state.value,
            )
        self.state.session_state = session_state

    def _persist_state(self) -> None:
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
    config = load_config(config_path)
    logger = configure_logging(config.runtime.log_namespace)
    daemon = DisplayDaemon(
        config=config,
        proxmox=ProxmoxClient(timeout_s=config.policy.command_timeout_s),
        logger=logger,
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
