from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
import logging
import os
import socket
import subprocess
import time

from .config import AppConfig, load_config
from .ipc import decode_message, encode_message, validate_daemon_message


DEFAULT_CONFIG_PATH = Path("/etc/relayinner-display/config.toml")


ProcessFactory = Callable[..., subprocess.Popen[str]]
PowerCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


WAITING_STATUS_TEXT = {
    "connecting": "Connecting",
    "vm_stopped": "Waiting for VM",
    "reconnecting": "Connection lost",
    "degraded": "Degraded",
}
DISPLAY_SLEEPING_STATUS = "Display sleeping"
DEFAULT_WAITING_STATUS = WAITING_STATUS_TEXT["vm_stopped"]
SESSION_ENV_ALLOWLIST = {
    "DBUS_SESSION_BUS_ADDRESS",
    "HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "PATH",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
}


def build_session_env(source_env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = source_env or os.environ
    env = {name: value for name, value in source.items() if name in SESSION_ENV_ALLOWLIST}
    env.setdefault("XDG_SESSION_TYPE", "wayland")
    return env


@dataclass
class SessionViewState:
    waiting_reason: str = "vm_stopped"
    status_text: str = DEFAULT_WAITING_STATUS
    console_active: bool = False
    cursor_hidden: bool = False
    display_power_state: str = "on"


class SessionSocketClient:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: socket.socket | None = None
        self.buffer = b""

    def connect(self) -> None:
        if self.connection is not None:
            return

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(self.path))
        connection.setblocking(False)
        self.connection = connection

    def read_messages(self) -> tuple[list[dict[str, Any]], bool]:
        if self.connection is None:
            return [], True

        messages: list[dict[str, Any]] = []
        disconnected = False
        while True:
            try:
                chunk = self.connection.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                disconnected = True
                self.close()
                break

            if not chunk:
                disconnected = True
                self.close()
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
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
                self.buffer = b""


class SessionSupervisor:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        power_helper: str = "wlopm",
        power_command_runner: PowerCommandRunner = subprocess.run,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(config.runtime.log_namespace)
        self.process_factory = process_factory
        self.power_helper = power_helper
        self.power_command_runner = power_command_runner
        self.viewer_process: subprocess.Popen[str] | None = None
        self.view_state = SessionViewState()
        self._suppress_exit_report = False

    def session_ready_message(self) -> dict[str, object]:
        return {"type": "session_ready"}

    def handle_daemon_message(self, message: dict[str, object]) -> list[dict[str, object]]:
        payload = validate_daemon_message(message)
        message_type = payload["type"]

        if message_type == "show_waiting":
            self.view_state.waiting_reason = str(payload["reason"])
            self._stop_console(report_exit=False)
            self._refresh_view_state()
            self.logger.info("Waiting state set to %s", self.view_state.waiting_reason)
            return []

        if message_type == "disconnect_console":
            self.view_state.waiting_reason = str(payload["reason"])
            self._stop_console(report_exit=False)
            self._refresh_view_state()
            self.logger.info("Console disconnected because %s", self.view_state.waiting_reason)
            return []

        if message_type == "connect_spice":
            vv_path = str(payload["vv_path"])
            return self._launch_console(vv_path)

        if message_type == "display_power":
            return self._apply_display_power(
                state=str(payload["state"]),
                output=str(payload["output"]),
            )

        if message_type == "health_ping":
            return []

        raise AssertionError(f"Unhandled daemon message type: {message_type}")

    def poll_console(self) -> dict[str, object] | None:
        if self.viewer_process is None:
            return None

        exit_status = self.viewer_process.poll()
        if exit_status is None:
            return None

        self.viewer_process = None
        self.view_state.waiting_reason = "reconnecting"
        self._refresh_view_state()
        if self._suppress_exit_report:
            self._suppress_exit_report = False
            return None

        event = {
            "type": "console_exited",
            "code": max(exit_status, 0),
            "signal": abs(exit_status) if exit_status < 0 else 0,
        }
        self.logger.warning("remote-viewer exited unexpectedly: %s", event)
        return event

    def _launch_console(self, vv_path: str) -> list[dict[str, object]]:
        self._stop_console(report_exit=False)
        self.view_state.waiting_reason = "connecting"
        command = ["remote-viewer", "--full-screen", vv_path]

        try:
            process = self.process_factory(
                command,
                env=build_session_env(),
                text=True,
            )
        except OSError as exc:
            reason = f"viewer_launch_failed: {exc}"
            self.view_state.waiting_reason = "degraded"
            self._refresh_view_state()
            self.logger.error("remote-viewer launch failed: %s", exc)
            return [{"type": "session_error", "reason": reason}]

        self.viewer_process = process
        self._suppress_exit_report = False
        self._refresh_view_state()
        self.logger.info("remote-viewer started with pid=%s", process.pid)
        return [{"type": "console_started", "pid": process.pid}]

    def _stop_console(self, report_exit: bool) -> None:
        if self.viewer_process is None:
            self._suppress_exit_report = False
            self._refresh_view_state()
            return

        self._suppress_exit_report = not report_exit
        self.viewer_process.terminate()
        try:
            self.viewer_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.viewer_process.kill()
            try:
                self.viewer_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.logger.warning("remote-viewer did not exit after SIGKILL")
        finally:
            self.viewer_process = None
            self._refresh_view_state()

    def _apply_display_power(self, state: str, output: str) -> list[dict[str, object]]:
        target_output = output.strip() or "*"
        command = [self.power_helper, "--on" if state == "on" else "--off", target_output]

        try:
            completed = self.power_command_runner(
                command,
                env=build_session_env(),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            self.logger.error("Display power helper failed to start: %s", exc)
            return []

        if completed.returncode != 0:
            helper_output = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {helper_output}" if helper_output else ""
            self.logger.error(
                "Display power helper exited with %s for state=%s output=%s%s",
                completed.returncode,
                state,
                target_output,
                suffix,
            )
            return []

        self.view_state.display_power_state = state
        self._refresh_view_state()
        self.logger.info("Display power applied: state=%s output=%s", state, target_output)
        return [{"type": "display_power_applied", "state": state}]

    def _refresh_view_state(self) -> None:
        self.view_state.console_active = self.viewer_process is not None
        self.view_state.cursor_hidden = self.viewer_process is not None
        if self.view_state.display_power_state == "off":
            self.view_state.status_text = DISPLAY_SLEEPING_STATUS
            return

        self.view_state.status_text = WAITING_STATUS_TEXT.get(
            self.view_state.waiting_reason,
            DEFAULT_WAITING_STATUS,
        )


def configure_logging(namespace: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger(namespace)


def run(config_path: Path) -> int:
    config = load_config(config_path)
    logger = configure_logging(config.runtime.log_namespace)
    supervisor = SessionSupervisor(config=config, logger=logger)
    client = SessionSocketClient(config.runtime.control_socket)
    poll_interval_s = config.policy.poll_interval_ms / 1000

    while True:
        try:
            client.connect()
        except OSError:
            time.sleep(poll_interval_s)
            continue

        if not client.send_message(supervisor.session_ready_message()):
            time.sleep(poll_interval_s)
            continue

        while True:
            messages, disconnected = client.read_messages()
            if disconnected:
                supervisor._stop_console(report_exit=False)
                break

            send_failed = False
            for message in messages:
                for response in supervisor.handle_daemon_message(message):
                    if not client.send_message(response):
                        send_failed = True
                        break
                if send_failed:
                    break

            if send_failed:
                supervisor._stop_console(report_exit=False)
                break

            exit_event = supervisor.poll_console()
            if exit_event is not None and not client.send_message(exit_event):
                supervisor._stop_console(report_exit=False)
                break

            time.sleep(poll_interval_s)

        client.close()
        time.sleep(poll_interval_s)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="relayinner-display-session")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
