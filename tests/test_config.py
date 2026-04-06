from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from relayinner_display.config import ConfigError, load_config


VALID_CONFIG = textwrap.dedent(
    """
    [target]
    vmid = 101
    node_name = "auto"
    guest_os = "windows"
    console_backend = "spice"

    [runtime]
    run_dir = "/run/relayinner-display"
    control_socket = "/run/relayinner-display/session.sock"
    log_namespace = "relayinner-display"

    [console]
    artifact_dir = "/run/relayinner-display/console"

    [console.spice]
    vv_path = "/run/relayinner-display/console/spice-current.vv"


    [policy]
    poll_interval_ms = 2000
    reconnect_initial_ms = 1000
    reconnect_max_ms = 15000
    command_timeout_s = 10
    """
)


class ConfigTests(unittest.TestCase):
    def test_load_config_accepts_spec_defaults(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(VALID_CONFIG, encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config.target.vmid, 101)
        self.assertEqual(config.target.node_name, "auto")
        self.assertEqual(config.runtime.control_socket.name, "session.sock")
        self.assertEqual(config.policy.reconnect_max_ms, 15000)
        self.assertEqual(config.policy.dpms_policy, "vm-power")
        self.assertEqual(config.policy.dpms_off_delay_ms, 5000)
        self.assertEqual(config.policy.power_state_stabilize_ms, 3000)
        self.assertEqual(config.display.output_name, "")
        self.assertEqual(config.display.power_helper, "wlr-randr")
        self.assertFalse(config.input.forward_power_button)
        self.assertEqual(config.input.debounce_ms, 2000)
        self.assertEqual(config.policy.power_button_action_when_running, "shutdown")
        self.assertEqual(config.policy.shutdown_timeout_s, 90)
        self.assertEqual(config.console.artifact_dir, Path("/run/relayinner-display/console"))
        self.assertEqual(
            config.console.spice.vv_path,
            Path("/run/relayinner-display/console/spice-current.vv"),
        )

    def test_load_config_accepts_display_overrides(self) -> None:
        content = VALID_CONFIG.replace(
            'command_timeout_s = 10\n',
            textwrap.dedent(
                """
                command_timeout_s = 10
                dpms_policy = "vm-power"
                dpms_off_delay_ms = 9000
                power_state_stabilize_ms = 1000

                [display]
                output_name = "HDMI-A-1"
                power_helper = "relay-wlopm"
                """
            ),
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config.display.output_name, "HDMI-A-1")
        self.assertEqual(config.display.power_helper, "relay-wlopm")
        self.assertEqual(config.policy.dpms_off_delay_ms, 9000)
        self.assertEqual(config.policy.power_state_stabilize_ms, 1000)

    def test_power_button_and_dpms_overrides_are_parsed(self) -> None:
        content = VALID_CONFIG.replace(
            "command_timeout_s = 10\n",
            textwrap.dedent(
                """
                command_timeout_s = 10
                dpms_off_delay_ms = 7000
                power_button_action_when_running = "shutdown"
                power_button_action_when_stopped = "start"
                shutdown_timeout_s = 120
                """
            ),
        ) + textwrap.dedent(
            """

            [display]
            output_name = "HDMI-A-1"

            [input]
            power_button_event = "/dev/input/by-path/platform-i8042-serio-0-event-power"
            forward_power_button = true
            debounce_ms = 3000
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config.display.output_name, "HDMI-A-1")
        self.assertTrue(config.input.forward_power_button)
        self.assertEqual(config.input.debounce_ms, 3000)
        self.assertEqual(config.policy.dpms_off_delay_ms, 7000)
        self.assertEqual(config.policy.shutdown_timeout_s, 120)

    def test_missing_required_key_raises(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"\n', "")
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_unsupported_console_backend_raises(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "rdp"')
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_accepts_vnc_and_looking_glass_backends(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "vnc"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.vnc]\ndisplay_number = 77\n",
        )

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.target.console_backend, "vnc")
        self.assertIsNotNone(config.console.vnc)
        self.assertIsNone(config.console.spice)
        self.assertEqual(config.console.vnc.bind_host, "127.0.0.1")
        self.assertEqual(config.console.vnc.display_number, 77)
        self.assertEqual(config.console.vnc.viewer, "remote-viewer")
        self.assertEqual(config.console.vnc.port, 5977)

        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.looking_glass]\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.target.console_backend, "looking-glass")
        self.assertIsNotNone(config.console.looking_glass)
        self.assertIsNone(config.console.spice)

    def test_legacy_runtime_spice_vv_path_is_accepted_for_spice_only(self) -> None:
        content = """
            [target]
            vmid = 101
            node_name = "auto"
            guest_os = "windows"
            console_backend = "spice"

            [runtime]
            run_dir = "/run/relayinner-display"
            control_socket = "/run/relayinner-display/session.sock"
            spice_vv_path = "/run/relayinner-display/legacy/current.vv"
            log_namespace = "relayinner-display"

            [policy]
            poll_interval_ms = 2000
            reconnect_initial_ms = 1000
            reconnect_max_ms = 15000
            command_timeout_s = 10
            """
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content.strip(), encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(
            config.runtime.spice_vv_path,
            Path("/run/relayinner-display/legacy/current.vv"),
        )
        self.assertEqual(config.console.spice.vv_path, Path("/run/relayinner-display/legacy/current.vv"))

    def test_runtime_spice_vv_path_rejected_for_non_spice_backends(self) -> None:
        content = textwrap.dedent(
            """
            [target]
            vmid = 101
            node_name = "auto"
            guest_os = "windows"
            console_backend = "vnc"

            [runtime]
            run_dir = "/run/relayinner-display"
            control_socket = "/run/relayinner-display/session.sock"
            spice_vv_path = "/run/relayinner-display/legacy/current.vv"
            log_namespace = "relayinner-display"

            [console]
            artifact_dir = "/run/relayinner-display/console"

            [console.vnc]
            display_number = 77
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_backend_specific_mismatch_rejected(self) -> None:
        content = VALID_CONFIG.replace(
            'console_backend = "spice"',
            'console_backend = "spice"',
        ).replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.vnc]\nfoo = 1\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_vnc_bind_host_must_be_loopback(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "vnc"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.vnc]\nbind_host = \"0.0.0.0\"\ndisplay_number = 77\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_vnc_display_number_must_be_in_range(self) -> None:
        for display_number in ("-1", "59636"):
            with self.subTest(display_number=display_number):
                content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "vnc"')
                content = content.replace(
                    "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
                    f"[console.vnc]\ndisplay_number = {display_number}\n",
                )
                with TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.toml"
                    config_path.write_text(content, encoding="utf-8")

                    with self.assertRaises(ConfigError):
                        load_config(config_path)

    def test_vnc_viewer_is_fixed_to_remote_viewer(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "vnc"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.vnc]\ndisplay_number = 77\nviewer = \"vinagre\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_invalid_dpms_policy_raises(self) -> None:
        content = VALID_CONFIG + '\ndpms_policy = "host-suspend"\n'
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_invalid_power_button_action_raises(self) -> None:
        content = VALID_CONFIG + '\npower_button_action_when_running = "poweroff"\n'
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
