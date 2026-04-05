from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from relayinner_display.proxmox import CommandResult, ProxmoxClient, ProxmoxCommandError


class ProxmoxTests(unittest.TestCase):
    def test_auto_node_resolution_prefers_local_hostname(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            self.assertEqual(timeout_s, 10)
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout=json.dumps([{"node": "pve-01"}, {"node": "pve-02"}]),
                stderr="",
            )

        client = ProxmoxClient(timeout_s=10, runner=runner, hostname_resolver=lambda: "pve-01")
        self.assertEqual(client.resolve_node_name("auto"), "pve-01")

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

    def test_request_spice_config_generates_vv_file(self) -> None:
        def runner(command: list[str], timeout_s: int) -> CommandResult:
            self.assertIn("--proxy", command)
            return CommandResult(
                args=tuple(command),
                returncode=0,
                stdout=json.dumps({"host": "127.0.0.1", "port": 61000, "title": "vm-101"}),
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
                    "ca": "-----BEGIN CERTIFICATE-----\nLINE2\n-----END CERTIFICATE-----\n",
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

        self.assertEqual(commands, [["qm", "start", "101"], ["qm", "shutdown", "101", "--timeout", "90"]])


if __name__ == "__main__":
    unittest.main()
