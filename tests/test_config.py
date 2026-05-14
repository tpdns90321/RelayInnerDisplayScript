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
        self.assertEqual(config.display.drm_compatibility, "auto")
        self.assertEqual(config.kiosk.compositor, "auto")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")
        self.assertFalse(config.input.forward_power_button)
        self.assertEqual(config.input.debounce_ms, 2000)
        self.assertEqual(config.policy.power_button_action_when_running, "shutdown")
        self.assertEqual(config.policy.shutdown_timeout_s, 90)
        self.assertEqual(config.console.artifact_dir, Path("/run/relayinner-display/console"))
        self.assertEqual(
            config.console.spice.vv_path,
            Path("/run/relayinner-display/console/spice-current.vv"),
        )

    def test_load_config_reports_missing_file_and_invalid_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.toml"
            with self.assertRaisesRegex(ConfigError, "Config file not found"):
                load_config(missing_path)

            invalid_path = Path(temp_dir) / "config.toml"
            invalid_path.write_text("[target\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "Invalid TOML"):
                load_config(invalid_path)

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
                drm_compatibility = "legacy-drm"
                """
            ),
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config.display.output_name, "HDMI-A-1")
        self.assertEqual(config.display.power_helper, "relay-wlopm")
        self.assertEqual(config.display.drm_compatibility, "legacy-drm")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")
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
        self.assertEqual(config.display.drm_compatibility, "auto")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")
        self.assertTrue(config.input.forward_power_button)
        self.assertEqual(config.input.debounce_ms, 3000)
        self.assertEqual(config.policy.dpms_off_delay_ms, 7000)
        self.assertEqual(config.policy.shutdown_timeout_s, 120)

    def test_invalid_display_drm_compatibility_raises(self) -> None:
        content = VALID_CONFIG + textwrap.dedent(
            """

            [display]
            drm_compatibility = "experimental"
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

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

    def test_accepts_vnc_looking_glass_and_moonlight_backends(self) -> None:
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
        self.assertEqual(config.kiosk.resolved_compositor, "cage")
        self.assertIsNotNone(config.console.vnc)
        self.assertIsNone(config.console.spice)
        self.assertEqual(config.console.vnc.bind_host, "127.0.0.1")
        self.assertEqual(config.console.vnc.display_number, 77)
        self.assertEqual(config.console.vnc.viewer, "remote-viewer")
        self.assertEqual(config.console.vnc.port, 5977)

        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            textwrap.dedent(
                """\
                [console.looking_glass]
                shm_file = "/dev/kvmfr0"
                renderer = "egl"
                fullscreen = false
                disable_host_screensaver = false
                spice_enabled = false
                """
            ),
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.target.console_backend, "looking-glass")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")
        self.assertIsNotNone(config.console.looking_glass)
        self.assertIsNone(config.console.spice)
        self.assertEqual(config.console.looking_glass.binary, "looking-glass-client")
        self.assertEqual(config.console.looking_glass.shm_file, Path("/dev/kvmfr0"))
        self.assertEqual(config.console.looking_glass.renderer, "egl")
        self.assertFalse(config.console.looking_glass.fullscreen)
        self.assertFalse(config.console.looking_glass.disable_host_screensaver)
        self.assertFalse(config.console.looking_glass.spice_enabled)

        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.moonlight]\nhost = \"192.168.50.20\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.target.console_backend, "moonlight")
        self.assertEqual(config.kiosk.compositor, "auto")
        self.assertEqual(config.kiosk.resolved_compositor, "sway")
        self.assertIsNotNone(config.console.moonlight)
        self.assertIsNone(config.console.spice)
        self.assertEqual(config.console.moonlight.binary, "moonlight")
        self.assertEqual(config.console.moonlight.host, "192.168.50.20")
        self.assertEqual(config.console.moonlight.base_port, 47989)
        self.assertEqual(config.console.moonlight.app, "Desktop")
        self.assertEqual(
            config.console.moonlight.state_dir,
            Path("/var/lib/relayinner-display/moonlight"),
        )
        self.assertIsNone(config.console.moonlight.resolution)
        self.assertFalse(config.console.moonlight.quit_app_after_session)
        self.assertEqual(config.console.moonlight.host_authority, "192.168.50.20")

    def test_looking_glass_defaults_are_applied(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.looking_glass]\nshm_file = \"/dev/kvmfr0\"\n",
        )

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.console.looking_glass.binary, "looking-glass-client")
        self.assertEqual(config.console.looking_glass.renderer, "auto")
        self.assertTrue(config.console.looking_glass.fullscreen)
        self.assertTrue(config.console.looking_glass.disable_host_screensaver)
        self.assertTrue(config.console.looking_glass.spice_enabled)

    def test_moonlight_custom_values_are_applied(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            textwrap.dedent(
                """\
                [console.moonlight]
                binary = "/usr/local/bin/moonlight"
                host = "2001:db8::20"
                base_port = 48010
                app = "Steam Big Picture"
                state_dir = "/var/lib/relayinner-display/custom-moonlight"
                resolution = " 3440X1440 "
                quit_app_after_session = true
                """
            ),
        )

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.console.moonlight.binary, "/usr/local/bin/moonlight")
        self.assertEqual(config.console.moonlight.host, "2001:db8::20")
        self.assertEqual(config.console.moonlight.base_port, 48010)
        self.assertEqual(config.console.moonlight.app, "Steam Big Picture")
        self.assertEqual(
            config.console.moonlight.state_dir,
            Path("/var/lib/relayinner-display/custom-moonlight"),
        )
        self.assertEqual(config.console.moonlight.resolution, "3440x1440")
        self.assertTrue(config.console.moonlight.quit_app_after_session)
        self.assertEqual(config.console.moonlight.host_authority, "[2001:db8::20]:48010")
        self.assertEqual(
            config.console.moonlight.portable_marker_path,
            Path("/var/lib/relayinner-display/custom-moonlight/portable.dat"),
        )
        self.assertEqual(
            config.console.moonlight.argv,
            [
                "/usr/local/bin/moonlight",
                "stream",
                "[2001:db8::20]:48010",
                "Steam Big Picture",
                "--resolution",
                "3440x1440",
                "--display-mode",
                "fullscreen",
                "--quit-after",
            ],
        )

    def test_kiosk_auto_is_explicitly_accepted(self) -> None:
        content = VALID_CONFIG + textwrap.dedent(
            """

            [kiosk]
            compositor = "auto"
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.kiosk.compositor, "auto")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")

    def test_kiosk_accepts_supported_explicit_backend_combinations(self) -> None:
        content = VALID_CONFIG + textwrap.dedent(
            """

            [kiosk]
            compositor = "cage"
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.kiosk.compositor, "cage")
        self.assertEqual(config.kiosk.resolved_compositor, "cage")

        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            textwrap.dedent(
                """\
                [kiosk]
                compositor = "sway"

                [console.moonlight]
                host = "192.168.50.20"
                """
            ),
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.kiosk.compositor, "sway")
        self.assertEqual(config.kiosk.resolved_compositor, "sway")

    def test_kiosk_rejects_unsupported_backend_combinations(self) -> None:
        invalid_configs = (
            VALID_CONFIG + textwrap.dedent(
                """

                [kiosk]
                compositor = "sway"
                """
            ),
            VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"').replace(
                "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
                textwrap.dedent(
                    """\
                    [kiosk]
                    compositor = "cage"

                    [console.moonlight]
                    host = "192.168.50.20"
                    """
                ),
            ),
        )

        for content in invalid_configs:
            with self.subTest(content=content):
                with TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.toml"
                    config_path.write_text(content, encoding="utf-8")

                    with self.assertRaises(ConfigError):
                        load_config(config_path)

    def test_kiosk_rejects_unknown_compositor_values(self) -> None:
        content = VALID_CONFIG + textwrap.dedent(
            """

            [kiosk]
            compositor = "river"
            """
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

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

    def test_looking_glass_renderer_must_be_supported(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.looking_glass]\nshm_file = \"/dev/kvmfr0\"\nrenderer = \"vulkan\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_looking_glass_shm_file_must_be_absolute(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.looking_glass]\nshm_file = \"dev/kvmfr0\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_looking_glass_binary_must_be_non_empty(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "looking-glass"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.looking_glass]\nshm_file = \"/dev/kvmfr0\"\nbinary = \"\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_moonlight_host_must_be_valid(self) -> None:
        for host in ("", "https://sunshine.example", "[2001:db8::20]", "bad host"):
            with self.subTest(host=host):
                content = VALID_CONFIG.replace(
                    'console_backend = "spice"',
                    'console_backend = "moonlight"',
                )
                content = content.replace(
                    "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
                    f"[console.moonlight]\nhost = \"{host}\"\n",
                )
                with TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.toml"
                    config_path.write_text(content, encoding="utf-8")

                    with self.assertRaises(ConfigError):
                        load_config(config_path)

    def test_moonlight_state_dir_must_be_absolute(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.moonlight]\nhost = \"192.168.50.20\"\nstate_dir = \"moonlight\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_moonlight_base_port_must_be_in_range(self) -> None:
        for base_port in ("0", "65536"):
            with self.subTest(base_port=base_port):
                content = VALID_CONFIG.replace(
                    'console_backend = "spice"',
                    'console_backend = "moonlight"',
                )
                content = content.replace(
                    "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
                    (
                        "[console.moonlight]\n"
                        "host = \"192.168.50.20\"\n"
                        f"base_port = {base_port}\n"
                    ),
                )
                with TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.toml"
                    config_path.write_text(content, encoding="utf-8")

                    with self.assertRaises(ConfigError):
                        load_config(config_path)

    def test_moonlight_binary_must_be_bare_name_or_absolute_path(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            "[console.moonlight]\nhost = \"192.168.50.20\"\nbinary = \"./moonlight\"\n",
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_moonlight_desktop_cannot_enable_quit_after_session(self) -> None:
        content = VALID_CONFIG.replace('console_backend = "spice"', 'console_backend = "moonlight"')
        content = content.replace(
            "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
            (
                "[console.moonlight]\n"
                "host = \"192.168.50.20\"\n"
                "app = \"Desktop\"\n"
                "quit_app_after_session = true\n"
            ),
        )
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_moonlight_resolution_must_be_valid(self) -> None:
        for resolution in ("1080p", "1920*1080", "1920 x 1080", "0x1080", "1920x0"):
            with self.subTest(resolution=resolution):
                content = VALID_CONFIG.replace(
                    'console_backend = "spice"',
                    'console_backend = "moonlight"',
                )
                content = content.replace(
                    "[console.spice]\nvv_path = \"/run/relayinner-display/console/spice-current.vv\"\n",
                    (
                        "[console.moonlight]\n"
                        "host = \"192.168.50.20\"\n"
                        f"resolution = \"{resolution}\"\n"
                    ),
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
