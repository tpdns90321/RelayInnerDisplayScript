from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import json
import os
import re
import socket
import subprocess
import tempfile


class ProxmoxCommandError(RuntimeError):
    """Raised when a local Proxmox CLI command fails or returns invalid data."""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


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
        self.hostname_resolver = hostname_resolver or (lambda: socket.gethostname().split(".")[0])
        self.fqdn_resolver = fqdn_resolver or socket.getfqdn

    def resolve_node_name(self, configured_name: str) -> str:
        if configured_name != "auto":
            return configured_name

        short_hostname = self.hostname_resolver()
        try:
            nodes = self._run_json(["pvesh", "get", "/nodes", "--output-format", "json"])
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
            raise ProxmoxCommandError(f"qm status returned unexpected output for VM {vmid}")
        return match.group(1)

    def start_vm(self, vmid: int) -> None:
        self._run(["qm", "start", str(vmid)])

    def shutdown_vm(self, vmid: int, timeout_s: int) -> None:
        command = ["qm", "shutdown", str(vmid)]
        if timeout_s > 0:
            command.extend(["--timeout", str(timeout_s)])
        self._run(command)

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
            lines.append(f"{key}={value}")
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
