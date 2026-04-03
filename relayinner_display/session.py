from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Callable
import logging
import os
import socket
import subprocess
import time

from .config import AppConfig, load_config
from .ipc import decode_message, encode_message, validate_daemon_message


DEFAULT_CONFIG_PATH = Path("/etc/relayinner-display/config.toml")


ProcessFactory = Callable[..., subprocess.Popen[str]]


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
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(config.runtime.log_namespace)
        self.process_factory = process_factory
        self.viewer_process: subprocess.Popen[str] | None = None
        self.waiting_reason = "vm_stopped"
        self._suppress_exit_report = False

    def session_ready_message(self) -> dict[str, object]:
        return {"type": "session_ready"}

    def handle_daemon_message(self, message: dict[str, object]) -> list[dict[str, object]]:
        payload = validate_daemon_message(message)
        message_type = payload["type"]

        if message_type == "show_waiting":
            self.waiting_reason = str(payload["reason"])
            self._stop_console(report_exit=False)
            self.logger.info("Waiting state set to %s", self.waiting_reason)
            return []

        if message_type == "disconnect_console":
            self.waiting_reason = str(payload["reason"])
            self._stop_console(report_exit=False)
            self.logger.info("Console disconnected because %s", self.waiting_reason)
            return []

        if message_type == "connect_spice":
            vv_path = str(payload["vv_path"])
            return self._launch_console(vv_path)

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
        self.waiting_reason = "connecting"
        command = ["remote-viewer", "--full-screen", vv_path]

        try:
            process = self.process_factory(
                command,
                env=self._build_viewer_env(),
                text=True,
            )
        except OSError as exc:
            reason = f"viewer_launch_failed: {exc}"
            self.waiting_reason = "degraded"
            self.logger.error("remote-viewer launch failed: %s", exc)
            return [{"type": "session_error", "reason": reason}]

        self.viewer_process = process
        self._suppress_exit_report = False
        self.logger.info("remote-viewer started with pid=%s", process.pid)
        return [{"type": "console_started", "pid": process.pid}]

    def _stop_console(self, report_exit: bool) -> None:
        if self.viewer_process is None:
            self._suppress_exit_report = False
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

    def _build_viewer_env(self) -> dict[str, str]:
        allowed_names = {
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
        return {name: value for name, value in os.environ.items() if name in allowed_names}


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
