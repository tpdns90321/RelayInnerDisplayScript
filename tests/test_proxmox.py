from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from relayinner_display.proxmox import (
    CommandResult,
    ProxmoxClient,
    default_runner,
    ProxmoxCommandError,
    VncConfigurationError,
    VncEndpointUnavailableError,
)


class ProxmoxTests(unittest.TestCase):
    def test_default_runner_returns_command_result(self) -> None:
        completed = type(
            "Completed",
            (),
            {"returncode": 2, "stdout": "out", "stderr": "err"},
        )()

        with patch("relayinner_display.proxmox.subprocess.run", return_value=completed) as run:
            result = default_runner(["qm", "status", "101"], timeout_s=7)

        run.assert_called_once_with(
            ["qm", "status", "101"],
            capture_output=True,
            text=True,
            timeout=7,
            check=False,
        )
        self.assertEqual(result, CommandResult(("qm", "status", "101"), 2, "out", "err"))

    def test_auto_node_resolution_prefers_local_hostname(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            self.assertEqual(timeout_s, 10)
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout=json.dumps([{"node": "pve-01"}, {"node": "pve-02"}]),
                stderr="",
            )

        client = ProxmoxClient(
            timeout_s=10, runner=runner, hostname_resolver=lambda: "pve-01"
        )
        self.assertEqual(client.resolve_node_name("auto"), "pve-01")

    def test_node_resolution_fallback_and_single_node_branches(self) -> None:
        self.assertEqual(ProxmoxClient(timeout_s=10).resolve_node_name("configured"), "configured")

        cases = [
            (CommandResult(("pvesh",), 1, "", "missing pvesh"), "local"),
            (CommandResult(("pvesh",), 0, json.dumps({"node": "pve"}), ""), "local"),
            (CommandResult(("pvesh",), 0, json.dumps(["bad", {"name": "remote"}]), ""), "remote"),
            (
                CommandResult(("pvesh",), 0, json.dumps([{"node": "remote-a"}, {"name": "remote-b"}]), ""),
                "local",
            ),
        ]

        for result, expected_node in cases:
            with self.subTest(expected_node=expected_node, stdout=result.stdout):
                client = ProxmoxClient(
                    timeout_s=10,
                    runner=lambda command, timeout_s, result=result: result,
                    hostname_resolver=lambda: "local",
                )
                self.assertEqual(client.resolve_node_name("auto"), expected_node)

    def test_get_vm_status_parses_qm_output(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="status: running\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        self.assertEqual(client.get_vm_status(101), "running")

    def test_get_vm_status_rejects_unexpected_qm_output(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="VM 101 is running\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        with self.assertRaisesRegex(ProxmoxCommandError, "unexpected output for VM 101"):
            client.get_vm_status(101)

    def test_request_spice_config_generates_vv_file(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            self.assertIn("--proxy", command)
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout=json.dumps(
                    {"host": "127.0.0.1", "port": 61000, "title": "vm-101"}
                ),
                stderr="",
            )

        client = ProxmoxClient(
            timeout_s=10,
            runner=runner,
            hostname_resolver=lambda: "pve-01",
            fqdn_resolver=lambda: "pve-01.example.test",
        )
        spice_config = client.request_spice_config("pve-01", 101)

        with TemporaryDirectory() as temp_dir:
            vv_path = Path(temp_dir) / "current.vv"
            client.write_vv_file(vv_path, spice_config)
            content = vv_path.read_text(encoding="utf-8")

        self.assertIn("[virt-viewer]", content)
        self.assertIn("host=127.0.0.1", content)
        self.assertIn("port=61000", content)

    def test_write_vv_file_escapes_multiline_values_for_ini_format(self) -> None:
        client = ProxmoxClient(timeout_s=10, runner=lambda command, timeout_s: None)  # type: ignore[arg-type]

        with TemporaryDirectory() as temp_dir:
            vv_path = Path(temp_dir) / "current.vv"
            client.write_vv_file(
                vv_path,
                {
                    "type": "spice",
                    "ca": "-----BEGIN CERTIFICATE-----\\nLINE2\\n-----END CERTIFICATE-----\\n",
                    "proxy": "http://127.0.0.1:3128",
                },
            )
            content = vv_path.read_text(encoding="utf-8")

        self.assertIn(
            "ca=-----BEGIN CERTIFICATE-----\\nLINE2\\n-----END CERTIFICATE-----\\n",
            content,
        )
        self.assertNotIn("ca=-----BEGIN CERTIFICATE-----\nLINE2", content)

    def test_nonzero_command_raises(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=1,
                stdout="",
                stderr="permission denied",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        with self.assertRaises(ProxmoxCommandError):
            client.get_vm_status(101)

    def test_power_button_vm_actions_use_expected_commands(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str], timeout_s: int) -> CommandResult:
            commands.append(command)
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        client.start_vm(101)
        client.shutdown_vm(101, timeout_s=90)

        self.assertEqual(
            commands,
            [["qm", "start", "101"], ["qm", "shutdown", "101", "--timeout", "90"]],
        )

    def test_validate_vnc_configuration_accepts_loopback_args(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            self.assertEqual(command, ["qm", "config", "101"])
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="name: win11\nargs: -device virtio-balloon -vnc 127.0.0.1:77,password=off\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        endpoint = client.validate_vnc_configuration(
            101,
            bind_host="localhost",
            display_number=77,
        )

        self.assertEqual(endpoint.bind_host, "127.0.0.1")
        self.assertEqual(endpoint.display_number, 77)
        self.assertEqual(endpoint.port, 5977)
        self.assertEqual(endpoint.endpoint, "127.0.0.1:5977")

    def test_validate_vnc_configuration_rejects_non_loopback_bind(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="args: -vnc 0.0.0.0:77\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        with self.assertRaises(VncConfigurationError):
            client.validate_vnc_configuration(101, bind_host="127.0.0.1", display_number=77)

    def test_validate_vnc_configuration_rejects_display_mismatch(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="args: -vnc 127.0.0.1:78\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        with self.assertRaises(VncConfigurationError):
            client.validate_vnc_configuration(101, bind_host="127.0.0.1", display_number=77)

    def test_validate_vnc_configuration_rejects_loopback_bind_host_mismatch(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout="args: -vnc localhost:77\n",
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner)
        with self.assertRaisesRegex(VncConfigurationError, "does not match relay config"):
            client.validate_vnc_configuration(101, bind_host="127.0.0.2", display_number=77)

    def test_probe_vnc_endpoint_reports_unreachable_socket(self) -> None:
        client = ProxmoxClient(timeout_s=10, runner=lambda command, timeout_s: None)  # type: ignore[arg-type]

        class FailingSocket:
            def __enter__(self) -> "FailingSocket":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def settimeout(self, timeout_s: float) -> None:
                return None

            def connect(self, address: tuple[str, int]) -> None:
                raise ConnectionRefusedError("refused")

        with patch("relayinner_display.proxmox.socket.socket", return_value=FailingSocket()):
            with self.assertRaises(VncEndpointUnavailableError):
                client.probe_vnc_endpoint("127.0.0.1", 5977)


if __name__ == "__main__":
    unittest.main()
