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

from .config import AppConfig, ConfigError, build_fallback_config, load_config
from .ipc import decode_message, encode_message, validate_daemon_message


DEFAULT_CONFIG_PATH = Path("/etc/relayinner-display/config.toml")


ProcessFactory = Callable[..., subprocess.Popen[str]]
PowerCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


WAITING_STATUS_TEXT = {
    "connecting": "Connecting",
    "vm_stopped": "Waiting for VM",
    "pairing_required": "Pairing required",
    "reconnecting": "Connection lost",
    "degraded": "Degraded",
}
DISPLAY_SLEEPING_STATUS = "Display sleeping"
DEFAULT_WAITING_STATUS = WAITING_STATUS_TEXT["vm_stopped"]
WLR_RANDR_HELPER = "wlr-randr"
CONSOLE_LAUNCHER_ALLOWLIST = {
    "spice": "remote-viewer",
    "vnc": "remote-viewer",
    "looking-glass": "looking-glass-client",
}
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


def subsystem_logger(namespace: str, subsystem: str) -> logging.Logger:
    return logging.getLogger(f"{namespace}.{subsystem}")


def display_power_helper_name(helper: str) -> str:
    return Path(helper).name or helper


def parse_wlr_randr_outputs(output: str) -> list[str]:
    outputs: list[str] = []
    seen: set[str] = set()

    for line in output.splitlines():
        if not line or line[:1].isspace():
            continue

        name = line.split(maxsplit=1)[0]
        if not name or any(not (char.isalnum() or char in "-._") for char in name):
            continue
        if name in seen:
            continue

        seen.add(name)
        outputs.append(name)

    return outputs


@dataclass
class SessionViewState:
    waiting_reason: str = "vm_stopped"
    status_text: str = DEFAULT_WAITING_STATUS
    details: dict[str, str] | None = None
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
        power_helper: str | None = None,
        power_command_runner: PowerCommandRunner = subprocess.run,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(config.runtime.log_namespace)
        self.namespace = self.logger.name
        self.session_logger = subsystem_logger(self.namespace, "session")
        self.console_logger = subsystem_logger(self.namespace, "console")
        self.display_logger = subsystem_logger(self.namespace, "display")
        self.process_factory = process_factory
        self.power_helper = power_helper or config.display.power_helper
        self.power_command_runner = power_command_runner
        self.console_process: subprocess.Popen[str] | None = None
        self.active_console_backend: str | None = None
        self.view_state = SessionViewState()
        self._suppress_exit_report = False
        self._send_legacy_console_events = False

    def session_ready_message(self) -> dict[str, object]:
        return {"type": "session_ready"}

    def handle_daemon_message(self, message: dict[str, object]) -> list[dict[str, object]]:
        payload = validate_daemon_message(message)
        message_type = payload["type"]

        if message_type == "show_waiting":
            details = (
                {str(key): str(value) for key, value in dict(payload["details"]).items()}
                if "details" in payload
                else None
            )
            self._show_waiting_state(reason=str(payload["reason"]), details=details)
            self.session_logger.info("Waiting state set to %s", self.view_state.waiting_reason)
            return []

        if message_type == "disconnect_console":
            self._show_waiting_state(reason=str(payload["reason"]))
            self.session_logger.info(
                "Console disconnected because %s",
                self.view_state.waiting_reason,
            )
            return []

        if message_type == "connect_console":
            return self._launch_console(
                backend=str(payload["backend"]),
                launcher=str(payload["launcher"]),
                argv=[str(argument) for argument in payload["argv"]],
                cwd=Path(str(payload["cwd"])) if "cwd" in payload else None,
            )

        if message_type == "connect_spice":
            return self._launch_console(
                backend="spice",
                launcher="remote-viewer",
                argv=["remote-viewer", "--full-screen", str(payload["vv_path"])],
                legacy_events=True,
            )

        if message_type == "display_power":
            return self._apply_display_power(
                state=str(payload["state"]),
                output=str(payload["output"]),
            )

        if message_type == "health_ping":
            return []

        raise AssertionError(f"Unhandled daemon message type: {message_type}")

    def poll_console(self) -> dict[str, object] | None:
        if self.console_process is None:
            return None

        exit_status = self.console_process.poll()
        if exit_status is None:
            return None

        backend = self.active_console_backend or self.config.target.console_backend
        self.console_process = None
        self.active_console_backend = None
        self.view_state.waiting_reason = "reconnecting"
        self._refresh_view_state()
        if self._suppress_exit_report:
            self._suppress_exit_report = False
            self._send_legacy_console_events = False
            return None

        event: dict[str, object] = {
            "type": "console_exited",
            "code": max(exit_status, 0),
            "signal": abs(exit_status) if exit_status < 0 else 0,
        }
        if not self._send_legacy_console_events:
            event["backend"] = backend
        self._send_legacy_console_events = False
        self.console_logger.warning("Console exited unexpectedly: backend=%s event=%s", backend, event)
        return event

    def _show_waiting_state(
        self,
        *,
        reason: str,
        details: dict[str, str] | None = None,
    ) -> None:
        self.view_state.waiting_reason = reason
        self.view_state.details = details
        self._stop_console(report_exit=False)
        self._refresh_view_state()

    def _launch_console(
        self,
        *,
        backend: str,
        launcher: str,
        argv: list[str],
        cwd: Path | None = None,
        legacy_events: bool = False,
    ) -> list[dict[str, object]]:
        rejection_reason = self._validate_console_request(
            backend=backend,
            launcher=launcher,
            argv=argv,
        )
        if rejection_reason is not None:
            self.view_state.waiting_reason = "degraded"
            self._refresh_view_state()
            self.console_logger.error("%s", rejection_reason)
            return [{"type": "session_error", "reason": rejection_reason}]

        self._stop_console(report_exit=False)
        self.view_state.waiting_reason = "connecting"
        self.view_state.details = None
        try:
            process = self.process_factory(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=build_session_env(),
                text=True,
            )
        except OSError as exc:
            reason = f"viewer_launch_failed: backend={backend}: {exc}"
            self.view_state.waiting_reason = "degraded"
            self._refresh_view_state()
            self.console_logger.error("Console launch failed: backend=%s error=%s", backend, exc)
            return [{"type": "session_error", "reason": reason}]

        self.console_process = process
        self.active_console_backend = backend
        self._suppress_exit_report = False
        self._send_legacy_console_events = legacy_events
        self._refresh_view_state()
        self.console_logger.info(
            "Console started: backend=%s launcher=%s pid=%s",
            backend,
            launcher,
            process.pid,
        )
        event: dict[str, object] = {"type": "console_started", "pid": process.pid}
        if not legacy_events:
            event["backend"] = backend
        return [event]

    def _stop_console(self, report_exit: bool) -> None:
        if self.console_process is None:
            self._suppress_exit_report = False
            self._send_legacy_console_events = False
            self.active_console_backend = None
            self._refresh_view_state()
            return

        self._suppress_exit_report = not report_exit
        process = self.console_process
        process.terminate()
        try:
            self._wait_for_console_process_exit(process)
        finally:
            self.console_process = None
            self.active_console_backend = None
            self._send_legacy_console_events = False
            self._refresh_view_state()

    def _wait_for_console_process_exit(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            process.kill()

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.console_logger.warning("Console process did not exit after SIGKILL")

    def _validate_console_request(
        self,
        *,
        backend: str,
        launcher: str,
        argv: list[str],
    ) -> str | None:
        configured_backend = self.config.target.console_backend
        if backend != configured_backend:
            return (
                "invalid_console_request: "
                f"backend={backend} does not match configured backend={configured_backend}"
            )

        allowed_launcher = CONSOLE_LAUNCHER_ALLOWLIST.get(backend)
        if backend == "moonlight":
            moonlight = self.config.console.moonlight
            if moonlight is None:
                return "invalid_console_request: backend=moonlight is not configured"
            if launcher != moonlight.binary or argv[0] != moonlight.binary:
                return (
                    "invalid_console_request: "
                    f"backend={backend} launcher={launcher} argv0={argv[0]}"
                )
            return None

        if allowed_launcher is None:
            return f"invalid_console_request: backend={backend} is not supported"

        launcher_name = Path(launcher).name or launcher
        argv_launcher_name = Path(argv[0]).name or argv[0]
        if launcher_name != allowed_launcher or argv_launcher_name != allowed_launcher:
            return (
                "invalid_console_request: "
                f"backend={backend} launcher={launcher} argv0={argv[0]}"
            )
        return None

    def _apply_display_power(self, state: str, output: str) -> list[dict[str, object]]:
        if display_power_helper_name(self.power_helper) == WLR_RANDR_HELPER:
            return self._apply_wlr_randr_display_power(state=state, output=output)
        return self._apply_generic_display_power(state=state, output=output)

    def _apply_generic_display_power(self, state: str, output: str) -> list[dict[str, object]]:
        target_output = output.strip() or "*"
        command = [self.power_helper, "--on" if state == "on" else "--off", target_output]
        completed = self._run_power_helper_command(
            command,
            context=f"for state={state} output={target_output}",
        )
        if completed is None:
            return []

        return self._record_display_power_applied(state=state, output=target_output)

    def _apply_wlr_randr_display_power(self, state: str, output: str) -> list[dict[str, object]]:
        target_outputs = self._resolve_wlr_randr_outputs(output)
        if not target_outputs:
            return []

        for target_output in target_outputs:
            command = [self.power_helper, "--output", target_output, "--on" if state == "on" else "--off"]
            completed = self._run_power_helper_command(
                command,
                context=f"for state={state} output={target_output}",
            )
            if completed is None:
                return []

        return self._record_display_power_applied(
            state=state,
            output=",".join(target_outputs),
        )

    def _record_display_power_applied(self, *, state: str, output: str) -> list[dict[str, object]]:
        self.view_state.display_power_state = state
        self._refresh_view_state()
        self.display_logger.info(
            "Display power applied: state=%s output=%s helper=%s",
            state,
            output,
            self.power_helper,
        )
        return [{"type": "display_power_applied", "state": state}]

    def _resolve_wlr_randr_outputs(self, output: str) -> list[str]:
        target_output = output.strip()
        if target_output:
            return [target_output]

        command = [self.power_helper]
        completed = self._run_power_helper_command(command, context="while listing outputs")
        if completed is None:
            return []

        outputs = parse_wlr_randr_outputs(completed.stdout or "")
        if not outputs:
            self.display_logger.error("Display power helper reported no outputs to control")
            return []

        return outputs

    def _run_power_helper_command(
        self,
        command: list[str],
        *,
        context: str,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            completed = self.power_command_runner(
                command,
                env=build_session_env(),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            self.display_logger.error("Display power helper failed to start: %s", exc)
            return None

        if completed.returncode != 0:
            helper_output = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {helper_output}" if helper_output else ""
            self.display_logger.error(
                "Display power helper exited with %s %s%s",
                completed.returncode,
                context,
                suffix,
            )
            return None

        return completed

    def _refresh_view_state(self) -> None:
        self.view_state.console_active = self.console_process is not None
        self.view_state.cursor_hidden = self.console_process is not None
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
    startup_error: str | None = None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        config = build_fallback_config()
        startup_error = f"Invalid config: {exc}"

    logger = configure_logging(config.runtime.log_namespace)
    if startup_error is not None:
        subsystem_logger(logger.name, "session").error("%s", startup_error)
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
