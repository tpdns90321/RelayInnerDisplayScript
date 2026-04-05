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
    spice_vv_path = "/run/relayinner-display/current.vv"
    log_namespace = "relayinner-display"

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
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "vnc"')
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
