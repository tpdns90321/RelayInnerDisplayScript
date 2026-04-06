from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Mapping, Sequence
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile


class ProxmoxCommandError(RuntimeError):
    """Raised when a local Proxmox CLI command fails or returns invalid data."""


class VncConfigurationError(RuntimeError):
    """Raised when VM config does not match the supported loopback-only VNC contract."""


class VncEndpointUnavailableError(RuntimeError):
    """Raised when a validated VNC endpoint is not reachable yet."""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class VncEndpoint:
    bind_host: str
    display_number: int

    @property
    def port(self) -> int:
        return 5900 + self.display_number

    @property
    def endpoint(self) -> str:
        return f"{self.bind_host}:{self.port}"


Runner = Callable[[Sequence[str], int], CommandResult]


def default_runner(command: Sequence[str], timeout_s: int) -> CommandResult:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return CommandResult(
        args=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class ProxmoxClient:
    def __init__(
        self,
        timeout_s: int,
        runner: Runner = default_runner,
        hostname_resolver: Callable[[], str] | None = None,
        fqdn_resolver: Callable[[], str] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.runner = runner
        self.hostname_resolver = hostname_resolver or (
            lambda: socket.gethostname().split(".")[0]
        )
        self.fqdn_resolver = fqdn_resolver or socket.getfqdn

    def resolve_node_name(self, configured_name: str) -> str:
        if configured_name != "auto":
            return configured_name

        short_hostname = self.hostname_resolver()
        try:
            nodes = self._run_json(
                ["pvesh", "get", "/nodes", "--output-format", "json"]
            )
        except ProxmoxCommandError:
            return short_hostname

        if not isinstance(nodes, list):
            return short_hostname

        node_names = []
        for entry in nodes:
            if not isinstance(entry, dict):
                continue
            name = entry.get("node") or entry.get("name")
            if isinstance(name, str):
                node_names.append(name)

        if short_hostname in node_names:
            return short_hostname
        if len(node_names) == 1:
            return node_names[0]
        return short_hostname

    def get_vm_status(self, vmid: int) -> str:
        result = self._run(["qm", "status", str(vmid)])
        match = re.search(r"status:\s+(\S+)", result.stdout)
        if not match:
            raise ProxmoxCommandError(
                f"qm status returned unexpected output for VM {vmid}"
            )
        return match.group(1)

    def start_vm(self, vmid: int) -> None:
        self._run(["qm", "start", str(vmid)])

    def shutdown_vm(self, vmid: int, timeout_s: int) -> None:
        command = ["qm", "shutdown", str(vmid)]
        if timeout_s > 0:
            command.extend(["--timeout", str(timeout_s)])
        self._run(command)

    def validate_vnc_configuration(
        self,
        vmid: int,
        *,
        bind_host: str,
        display_number: int,
    ) -> VncEndpoint:
        endpoint = self.read_vnc_endpoint(vmid)
        if endpoint.bind_host not in {"127.0.0.1", "localhost"}:
            raise VncConfigurationError(
                f"VM config exposes VNC on non-loopback bind_host={endpoint.bind_host!r}"
            )
        if self._normalize_loopback_host(endpoint.bind_host) != self._normalize_loopback_host(
            bind_host
        ):
            raise VncConfigurationError(
                "VM config VNC bind_host "
                f"{endpoint.bind_host!r} does not match relay config bind_host={bind_host!r}"
            )
        if endpoint.display_number != display_number:
            raise VncConfigurationError(
                "VM config VNC display_number "
                f"{endpoint.display_number} does not match relay config display_number={display_number}"
            )
        return endpoint

    def read_vnc_endpoint(self, vmid: int) -> VncEndpoint:
        result = self._run(["qm", "config", str(vmid)])
        args_value: str | None = None
        for line in result.stdout.splitlines():
            if not line.startswith("args:"):
                continue
            _, _, raw_value = line.partition(":")
            args_value = raw_value.strip()
            break

        if not args_value:
            raise VncConfigurationError(
                "VM config does not expose a VNC endpoint through `args: -vnc ...`"
            )

        try:
            argv = shlex.split(args_value)
        except ValueError as exc:
            raise ProxmoxCommandError(f"qm config returned invalid args for VM {vmid}") from exc

        for index, token in enumerate(argv):
            if token != "-vnc":
                continue
            if index + 1 >= len(argv):
                raise VncConfigurationError("VM config is missing a VNC endpoint after `-vnc`")
            return self._parse_vnc_endpoint(argv[index + 1])

        raise VncConfigurationError(
            "VM config does not expose a VNC endpoint through `args: -vnc ...`"
        )

    def probe_vnc_endpoint(self, bind_host: str, port: int, timeout_s: float = 1.0) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout_s)
                connection.connect((bind_host, port))
        except OSError as exc:
            raise VncEndpointUnavailableError(
                f"VNC endpoint {bind_host}:{port} is not reachable yet: {exc}"
            ) from exc

    def request_spice_config(
        self,
        node_name: str,
        vmid: int,
        proxy_host: str | None = None,
    ) -> dict[str, str]:
        proxy = proxy_host or self.fqdn_resolver()
        payload = self._run_json(
            [
                "pvesh",
                "create",
                f"/nodes/{node_name}/qemu/{vmid}/spiceproxy",
                "--output-format",
                "json",
                "--proxy",
                proxy,
            ]
        )
        if not isinstance(payload, dict):
            raise ProxmoxCommandError("spiceproxy did not return a JSON object")
        return {str(key): str(value) for key, value in payload.items()}

    def write_vv_file(self, path: Path, spice_config: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["[virt-viewer]"]
        for key, value in sorted(spice_config.items()):
            # if you want to debug this, uncomment below
            # if key == "delete-this-file":
            #     continue
            if key == "proxy":
                port = urlsplit(value).port
                value = f"http://127.0.0.1:{port}"
            serialized_value = str(value)
            lines.append(f"{key}={serialized_value}")
        self._atomic_write(path, "\n".join(lines) + "\n")

    def _run(self, command: Sequence[str]) -> CommandResult:
        result = self.runner(command, self.timeout_s)
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip().splitlines()
            summary = details[0] if details else "no command output"
            raise ProxmoxCommandError(f"{command[0]} failed: {summary}")
        return result

    def _run_json(self, command: Sequence[str]) -> Any:
        result = self._run(command)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProxmoxCommandError(f"{command[0]} returned invalid JSON") from exc

    def _atomic_write(self, path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)

        os.replace(temp_path, path)

    def _parse_vnc_endpoint(self, value: str) -> VncEndpoint:
        endpoint_value = value.split(",", 1)[0]
        if ":" not in endpoint_value:
            raise VncConfigurationError(f"Unsupported VNC endpoint syntax: {value!r}")

        bind_host, display_text = endpoint_value.rsplit(":", 1)
        if not bind_host:
            raise VncConfigurationError(f"Unsupported VNC endpoint syntax: {value!r}")

        try:
            display_number = int(display_text)
        except ValueError as exc:
            raise VncConfigurationError(f"Unsupported VNC display number in {value!r}") from exc

        if display_number < 0:
            raise VncConfigurationError(f"Unsupported VNC display number in {value!r}")

        return VncEndpoint(bind_host=bind_host, display_number=display_number)

    def _normalize_loopback_host(self, host: str) -> str:
        if host in {"127.0.0.1", "localhost"}:
            return "127.0.0.1"
        return host
