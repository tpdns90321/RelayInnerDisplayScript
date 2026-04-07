from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
from typing import Any
import tomllib


SUPPORTED_CONSOLE_BACKENDS = {"spice", "vnc", "looking-glass", "moonlight"}
SUPPORTED_KIOSK_COMPOSITORS = {"auto", "cage", "sway"}
DEFAULT_KIOSK_COMPOSITOR = "auto"
DEFAULT_RESOLVED_KIOSK_COMPOSITOR_BY_BACKEND = {
    "looking-glass": "cage",
    "moonlight": "sway",
    "spice": "cage",
    "vnc": "cage",
}
SUPPORTED_KIOSK_COMPOSITORS_BY_BACKEND = {
    "looking-glass": {"cage"},
    "moonlight": {"sway"},
    "spice": {"cage"},
    "vnc": {"cage"},
}
SUPPORTED_VNC_BIND_HOSTS = {"127.0.0.1", "localhost"}
SUPPORTED_VNC_VIEWERS = {"remote-viewer"}
SUPPORTED_LOOKING_GLASS_RENDERERS = {"auto", "egl", "opengl"}
SUPPORTED_DPMS_POLICIES = {"vm-power"}
SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_RUNNING = {"shutdown"}
SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_STOPPED = {"start"}
DEFAULT_POWER_BUTTON_EVENT = Path("/dev/input/by-path/platform-i8042-serio-0-event-power")
DEFAULT_RUNTIME_RUN_DIR = Path("/run/relayinner-display")
DEFAULT_CONTROL_SOCKET = DEFAULT_RUNTIME_RUN_DIR / "session.sock"
DEFAULT_CONSOLE_ARTIFACT_DIR = DEFAULT_RUNTIME_RUN_DIR / "console"
DEFAULT_SPICE_VV_PATH = DEFAULT_CONSOLE_ARTIFACT_DIR / "spice-current.vv"
DEFAULT_LOG_NAMESPACE = "relayinner-display"
DEFAULT_VNC_BIND_HOST = "127.0.0.1"
DEFAULT_VNC_VIEWER = "remote-viewer"
DEFAULT_LOOKING_GLASS_BINARY = "looking-glass-client"
DEFAULT_LOOKING_GLASS_RENDERER = "auto"
DEFAULT_MOONLIGHT_BINARY = "moonlight"
DEFAULT_MOONLIGHT_BASE_PORT = 47989
DEFAULT_MOONLIGHT_APP = "Desktop"
DEFAULT_MOONLIGHT_STATE_DIR = Path("/var/lib/relayinner-display/moonlight")
MAX_VNC_DISPLAY_NUMBER = 65535 - 5900
MAX_PORT_NUMBER = 65535
HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
MOONLIGHT_RESOLUTION_RE = re.compile(r"^(?P<width>\d+)[xX](?P<height>\d+)$")


class ConfigError(ValueError):
    """Raised when the runtime configuration is invalid."""


@dataclass(frozen=True)
class TargetConfig:
    vmid: int
    node_name: str
    guest_os: str
    console_backend: str


@dataclass(frozen=True)
class RuntimeConfig:
    run_dir: Path
    control_socket: Path
    spice_vv_path: Path
    log_namespace: str

    @property
    def daemon_state_path(self) -> Path:
        return self.run_dir / "daemon.state.json"


@dataclass(frozen=True)
class DisplayConfig:
    output_name: str = ""
    power_helper: str = "wlr-randr"


@dataclass(frozen=True)
class KioskConfig:
    compositor: str = DEFAULT_KIOSK_COMPOSITOR
    resolved_compositor: str = "cage"


@dataclass(frozen=True)
class InputConfig:
    power_button_event: Path = DEFAULT_POWER_BUTTON_EVENT
    forward_power_button: bool = False
    debounce_ms: int = 2000


@dataclass(frozen=True)
class PolicyConfig:
    poll_interval_ms: int
    reconnect_initial_ms: int
    reconnect_max_ms: int
    command_timeout_s: int
    dpms_policy: str = "vm-power"
    dpms_off_delay_ms: int = 5000
    power_state_stabilize_ms: int = 3000
    power_button_action_when_running: str = "shutdown"
    power_button_action_when_stopped: str = "start"
    shutdown_timeout_s: int = 90


@dataclass(frozen=True)
class ConsoleSpiceConfig:
    vv_path: Path


@dataclass(frozen=True)
class ConsoleVncConfig:
    display_number: int
    bind_host: str = DEFAULT_VNC_BIND_HOST
    viewer: str = DEFAULT_VNC_VIEWER

    @property
    def port(self) -> int:
        return 5900 + self.display_number

    @property
    def endpoint(self) -> str:
        return f"{self.bind_host}:{self.port}"

    @property
    def uri(self) -> str:
        return f"vnc://{self.endpoint}"


@dataclass(frozen=True)
class ConsoleLookingGlassConfig:
    shm_file: Path
    binary: str = DEFAULT_LOOKING_GLASS_BINARY
    renderer: str = DEFAULT_LOOKING_GLASS_RENDERER
    fullscreen: bool = True
    disable_host_screensaver: bool = True
    spice_enabled: bool = True

    @property
    def argv(self) -> list[str]:
        argv = [self.binary]
        if self.fullscreen:
            argv.append("-F")
        if self.disable_host_screensaver:
            argv.append("-S")
        argv.extend(["-g", self.renderer, "-f", str(self.shm_file)])
        if not self.spice_enabled:
            argv.append("-s")
        return argv


@dataclass(frozen=True)
class ConsoleMoonlightConfig:
    host: str
    binary: str = DEFAULT_MOONLIGHT_BINARY
    base_port: int = DEFAULT_MOONLIGHT_BASE_PORT
    app: str = DEFAULT_MOONLIGHT_APP
    state_dir: Path = DEFAULT_MOONLIGHT_STATE_DIR
    resolution: str | None = None
    quit_app_after_session: bool = False

    def __post_init__(self) -> None:
        if self.quit_app_after_session and self.app.casefold() == DEFAULT_MOONLIGHT_APP.casefold():
            raise ConfigError(
                "console.moonlight.quit_app_after_session=true is not valid when "
                "console.moonlight.app='Desktop'"
            )

    @property
    def host_authority(self) -> str:
        return _render_moonlight_host_authority(self.host, self.base_port)

    @property
    def portable_marker_path(self) -> Path:
        return self.state_dir / "portable.dat"

    @property
    def argv(self) -> list[str]:
        argv = [
            self.binary,
            "stream",
            self.host_authority,
            self.app,
        ]
        if self.resolution is not None:
            argv.extend(["--resolution", self.resolution])
        argv.extend(["--display-mode", "fullscreen"])
        if self.quit_app_after_session:
            argv.append("--quit-after")
        return argv


@dataclass(frozen=True)
class ConsoleConfig:
    artifact_dir: Path
    spice: ConsoleSpiceConfig | None = None
    vnc: ConsoleVncConfig | None = None
    looking_glass: ConsoleLookingGlassConfig | None = None
    moonlight: ConsoleMoonlightConfig | None = None


@dataclass(frozen=True)
class AppConfig:
    target: TargetConfig
    runtime: RuntimeConfig
    policy: PolicyConfig
    display: DisplayConfig = field(default_factory=DisplayConfig)
    kiosk: KioskConfig = field(default_factory=KioskConfig)
    input: InputConfig = field(default_factory=InputConfig)
    console: ConsoleConfig = field(
        default_factory=lambda: ConsoleConfig(artifact_dir=DEFAULT_CONSOLE_ARTIFACT_DIR),
    )


def build_fallback_config() -> AppConfig:
    return AppConfig(
        target=TargetConfig(
            vmid=0,
            node_name="unknown",
            guest_os="unknown",
            console_backend="spice",
        ),
        runtime=RuntimeConfig(
            run_dir=DEFAULT_RUNTIME_RUN_DIR,
            control_socket=DEFAULT_CONTROL_SOCKET,
            spice_vv_path=DEFAULT_SPICE_VV_PATH,
            log_namespace=DEFAULT_LOG_NAMESPACE,
        ),
        console=ConsoleConfig(
            artifact_dir=DEFAULT_CONSOLE_ARTIFACT_DIR,
            spice=ConsoleSpiceConfig(vv_path=DEFAULT_SPICE_VV_PATH),
        ),
        policy=PolicyConfig(
            poll_interval_ms=2000,
            reconnect_initial_ms=1000,
            reconnect_max_ms=15000,
            command_timeout_s=10,
            dpms_policy="vm-power",
            dpms_off_delay_ms=5000,
            power_state_stabilize_ms=3000,
            power_button_action_when_running="shutdown",
            power_button_action_when_stopped="start",
            shutdown_timeout_s=90,
        ),
        display=DisplayConfig(),
        kiosk=KioskConfig(),
        input=InputConfig(forward_power_button=False),
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> AppConfig:
    target_table = _require_table(raw, "target")
    runtime_table = _require_table(raw, "runtime")
    policy_table = _require_table(raw, "policy")
    display_table = _optional_table(raw, "display")
    kiosk_table = _optional_table(raw, "kiosk")
    input_table = _optional_table(raw, "input")
    console_table = _optional_table(raw, "console")

    target = TargetConfig(
        vmid=_require_positive_int(target_table, "vmid"),
        node_name=_require_non_empty_string(target_table, "node_name"),
        guest_os=_require_non_empty_string(target_table, "guest_os"),
        console_backend=_require_non_empty_string(target_table, "console_backend"),
    )
    if target.console_backend not in SUPPORTED_CONSOLE_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_CONSOLE_BACKENDS))
        raise ConfigError(
            f"Unsupported console_backend={target.console_backend!r}; supported values: {supported}"
        )

    legacy_spice_vv_path = _optional_absolute_path_or_none(runtime_table, "spice_vv_path")
    if target.console_backend != "spice" and legacy_spice_vv_path is not None:
        raise ConfigError(
            "runtime.spice_vv_path is only supported for console_backend='spice'"
        )

    console = _parse_console_config(
        backend=target.console_backend,
        table=console_table,
        legacy_spice_vv_path=legacy_spice_vv_path,
    )
    runtime_spice_vv_path = (
        legacy_spice_vv_path
        if legacy_spice_vv_path is not None
        else (
            console.spice.vv_path
            if console.spice is not None
            else console.artifact_dir / "spice-current.vv"
        )
    )

    runtime = RuntimeConfig(
        run_dir=_require_absolute_path(runtime_table, "run_dir"),
        control_socket=_require_absolute_path(runtime_table, "control_socket"),
        spice_vv_path=runtime_spice_vv_path,
        log_namespace=_require_non_empty_string(runtime_table, "log_namespace"),
    )
    _require_child_path(runtime.run_dir, runtime.control_socket, "runtime.control_socket")
    _require_child_path(runtime.run_dir, console.artifact_dir, "console.artifact_dir")
    _require_child_path(runtime.run_dir, runtime.spice_vv_path, "runtime.spice_vv_path")

    display = DisplayConfig(
        output_name=_optional_string(display_table, "output_name", default=""),
        power_helper=_optional_non_empty_string(display_table, "power_helper", default="wlr-randr"),
    )
    kiosk = _parse_kiosk_config(target.console_backend, kiosk_table)

    input_config = InputConfig(
        power_button_event=_optional_absolute_path(
            input_table,
            "power_button_event",
            default=DEFAULT_POWER_BUTTON_EVENT,
        ),
        forward_power_button=_optional_bool(input_table, "forward_power_button", default=False),
        debounce_ms=_optional_non_negative_int(input_table, "debounce_ms", default=2000),
    )

    policy = PolicyConfig(
        poll_interval_ms=_require_positive_int(policy_table, "poll_interval_ms"),
        reconnect_initial_ms=_require_positive_int(policy_table, "reconnect_initial_ms"),
        reconnect_max_ms=_require_positive_int(policy_table, "reconnect_max_ms"),
        command_timeout_s=_require_positive_int(policy_table, "command_timeout_s"),
        dpms_policy=_optional_non_empty_string(policy_table, "dpms_policy", default="vm-power"),
        dpms_off_delay_ms=_optional_non_negative_int(
            policy_table,
            "dpms_off_delay_ms",
            default=5000,
        ),
        power_state_stabilize_ms=_optional_non_negative_int(
            policy_table,
            "power_state_stabilize_ms",
            default=3000,
        ),
        power_button_action_when_running=_optional_non_empty_string(
            policy_table,
            "power_button_action_when_running",
            default="shutdown",
        ),
        power_button_action_when_stopped=_optional_non_empty_string(
            policy_table,
            "power_button_action_when_stopped",
            default="start",
        ),
        shutdown_timeout_s=_optional_non_negative_int(
            policy_table,
            "shutdown_timeout_s",
            default=90,
        ),
    )
    if policy.reconnect_initial_ms > policy.reconnect_max_ms:
        raise ConfigError("policy.reconnect_initial_ms must be <= policy.reconnect_max_ms")
    if policy.dpms_policy not in SUPPORTED_DPMS_POLICIES:
        supported = ", ".join(sorted(SUPPORTED_DPMS_POLICIES))
        raise ConfigError(
            f"Unsupported dpms_policy={policy.dpms_policy!r}; supported values: {supported}"
        )
    if policy.power_button_action_when_running not in SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_RUNNING:
        supported = ", ".join(sorted(SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_RUNNING))
        raise ConfigError(
            "Unsupported power_button_action_when_running="
            f"{policy.power_button_action_when_running!r}; supported values: {supported}"
        )
    if policy.power_button_action_when_stopped not in SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_STOPPED:
        supported = ", ".join(sorted(SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_STOPPED))
        raise ConfigError(
            "Unsupported power_button_action_when_stopped="
            f"{policy.power_button_action_when_stopped!r}; supported values: {supported}"
        )

    return AppConfig(
        target=target,
        runtime=runtime,
        policy=policy,
        display=display,
        kiosk=kiosk,
        input=input_config,
        console=console,
    )


def _require_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing required [{key}] table")
    return value


def _optional_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid [{key}] table")
    return value


def _require_non_empty_string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or invalid string value for {key!r}")
    return value


def _optional_string(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"Missing or invalid string value for {key!r}")
    return value


def _optional_non_empty_string(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or invalid string value for {key!r}")
    return value


def _require_positive_int(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Missing or invalid positive integer value for {key!r}")
    return value


def _optional_non_negative_int(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"Missing or invalid non-negative integer value for {key!r}")
    return value


def _optional_bool(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Missing or invalid boolean value for {key!r}")
    return value


def _require_absolute_path(table: dict[str, Any], key: str) -> Path:
    value = _require_non_empty_string(table, key)
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{key!r} must be an absolute path")
    return path


def _optional_absolute_path(table: dict[str, Any], key: str, default: Path) -> Path:
    value = table.get(key, str(default))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or invalid string value for {key!r}")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{key!r} must be an absolute path")
    return path


def _parse_kiosk_config(backend: str, table: dict[str, Any]) -> KioskConfig:
    _require_only_keys(table, {"compositor"}, "kiosk")

    compositor = _optional_non_empty_string(
        table,
        "compositor",
        default=DEFAULT_KIOSK_COMPOSITOR,
    )
    if compositor not in SUPPORTED_KIOSK_COMPOSITORS:
        supported = ", ".join(sorted(SUPPORTED_KIOSK_COMPOSITORS))
        raise ConfigError(
            f"Unsupported kiosk.compositor={compositor!r}; supported values: {supported}"
        )

    resolved_compositor = resolve_kiosk_compositor(backend, compositor)
    return KioskConfig(
        compositor=compositor,
        resolved_compositor=resolved_compositor,
    )


def resolve_kiosk_compositor(backend: str, compositor: str) -> str:
    if compositor == DEFAULT_KIOSK_COMPOSITOR:
        return DEFAULT_RESOLVED_KIOSK_COMPOSITOR_BY_BACKEND[backend]

    supported = SUPPORTED_KIOSK_COMPOSITORS_BY_BACKEND[backend]
    if compositor not in supported:
        supported_values = ", ".join(sorted(supported))
        raise ConfigError(
            f"Unsupported kiosk.compositor={compositor!r} for console_backend={backend!r}; "
            f"supported values for this backend: {supported_values}"
        )
    return compositor


def _parse_console_config(
    backend: str,
    table: dict[str, Any],
    legacy_spice_vv_path: Path | None = None,
) -> ConsoleConfig:
    artifact_dir = _optional_absolute_path(table, "artifact_dir", default=DEFAULT_CONSOLE_ARTIFACT_DIR)
    allowed_console_keys = {"artifact_dir", "spice", "vnc", "looking_glass", "moonlight"}
    _require_subset_keys(table, "console", allowed_console_keys)

    spice_block = _optional_table(table, "spice")
    vnc_block = _optional_table(table, "vnc")
    looking_glass_block = _optional_table(table, "looking_glass")
    moonlight_block = _optional_table(table, "moonlight")

    if backend == "spice":
        _require_empty_table(looking_glass_block, backend, "console.looking_glass")
        _require_empty_table(vnc_block, backend, "console.vnc")
        _require_empty_table(moonlight_block, backend, "console.moonlight")
        _require_only_keys(spice_block, {"vv_path"}, "console.spice")

        if spice_block:
            vv_path = _require_absolute_path(spice_block, "vv_path")
        elif legacy_spice_vv_path is not None:
            vv_path = legacy_spice_vv_path
        else:
            vv_path = artifact_dir / "spice-current.vv"
        spice = ConsoleSpiceConfig(vv_path=vv_path)
        return ConsoleConfig(artifact_dir=artifact_dir, spice=spice)

    if backend == "vnc":
        _require_empty_table(spice_block, backend, "console.spice")
        _require_empty_table(looking_glass_block, backend, "console.looking_glass")
        _require_empty_table(moonlight_block, backend, "console.moonlight")
        _require_only_keys(vnc_block, {"bind_host", "display_number", "viewer"}, "console.vnc")

        bind_host = _optional_non_empty_string(
            vnc_block,
            "bind_host",
            default=DEFAULT_VNC_BIND_HOST,
        )
        if bind_host not in SUPPORTED_VNC_BIND_HOSTS:
            supported = ", ".join(sorted(SUPPORTED_VNC_BIND_HOSTS))
            raise ConfigError(
                f"Unsupported console.vnc.bind_host={bind_host!r}; supported values: {supported}"
            )

        display_number = _require_vnc_display_number(vnc_block, "display_number")
        viewer = _optional_non_empty_string(vnc_block, "viewer", default=DEFAULT_VNC_VIEWER)
        if viewer not in SUPPORTED_VNC_VIEWERS:
            supported = ", ".join(sorted(SUPPORTED_VNC_VIEWERS))
            raise ConfigError(
                f"Unsupported console.vnc.viewer={viewer!r}; supported values: {supported}"
            )

        return ConsoleConfig(
            artifact_dir=artifact_dir,
            vnc=ConsoleVncConfig(
                display_number=display_number,
                bind_host=bind_host,
                viewer=viewer,
            ),
        )

    if backend == "looking-glass":
        _require_empty_table(spice_block, backend, "console.spice")
        _require_empty_table(vnc_block, backend, "console.vnc")
        _require_empty_table(moonlight_block, backend, "console.moonlight")
        _require_only_keys(
            looking_glass_block,
            {
                "binary",
                "shm_file",
                "renderer",
                "fullscreen",
                "disable_host_screensaver",
                "spice_enabled",
            },
            "console.looking_glass",
        )

        renderer = _optional_non_empty_string(
            looking_glass_block,
            "renderer",
            default=DEFAULT_LOOKING_GLASS_RENDERER,
        )
        if renderer not in SUPPORTED_LOOKING_GLASS_RENDERERS:
            supported = ", ".join(sorted(SUPPORTED_LOOKING_GLASS_RENDERERS))
            raise ConfigError(
                "Unsupported console.looking_glass.renderer="
                f"{renderer!r}; supported values: {supported}"
            )

        return ConsoleConfig(
            artifact_dir=artifact_dir,
            looking_glass=ConsoleLookingGlassConfig(
                binary=_optional_non_empty_string(
                    looking_glass_block,
                    "binary",
                    default=DEFAULT_LOOKING_GLASS_BINARY,
                ),
                shm_file=_require_absolute_path(looking_glass_block, "shm_file"),
                renderer=renderer,
                fullscreen=_optional_bool(looking_glass_block, "fullscreen", default=True),
                disable_host_screensaver=_optional_bool(
                    looking_glass_block,
                    "disable_host_screensaver",
                    default=True,
                ),
                spice_enabled=_optional_bool(looking_glass_block, "spice_enabled", default=True),
            ),
        )

    _require_empty_table(spice_block, backend, "console.spice")
    _require_empty_table(vnc_block, backend, "console.vnc")
    _require_empty_table(looking_glass_block, backend, "console.looking_glass")
    _require_only_keys(
        moonlight_block,
        {
            "binary",
            "host",
            "base_port",
            "app",
            "state_dir",
            "resolution",
            "quit_app_after_session",
        },
        "console.moonlight",
    )

    return ConsoleConfig(
        artifact_dir=artifact_dir,
        moonlight=ConsoleMoonlightConfig(
            binary=_optional_command_or_absolute_path(
                moonlight_block,
                "binary",
                default=DEFAULT_MOONLIGHT_BINARY,
            ),
            host=_require_moonlight_host(moonlight_block, "host"),
            base_port=_optional_port(
                moonlight_block,
                "base_port",
                default=DEFAULT_MOONLIGHT_BASE_PORT,
            ),
            app=_optional_non_empty_string(
                moonlight_block,
                "app",
                default=DEFAULT_MOONLIGHT_APP,
            ),
            state_dir=_optional_absolute_path(
                moonlight_block,
                "state_dir",
                default=DEFAULT_MOONLIGHT_STATE_DIR,
            ),
            resolution=_optional_moonlight_resolution(moonlight_block, "resolution"),
            quit_app_after_session=_optional_bool(
                moonlight_block,
                "quit_app_after_session",
                default=False,
            ),
        ),
    )


def _require_empty_table(block: dict[str, Any], backend: str, label: str) -> None:
    if block:
        raise ConfigError(f"{label} is not valid for console_backend={backend!r}")


def _require_only_keys(table: dict[str, Any], allowed_keys: set[str], label: str) -> None:
    unexpected = sorted(set(table) - allowed_keys)
    if unexpected:
        extras = ", ".join(unexpected)
        raise ConfigError(f"Unexpected field(s) for {label}: {extras}")


def _require_subset_keys(table: dict[str, Any], label: str, allowed_keys: set[str]) -> None:
    unexpected = sorted(set(table) - allowed_keys)
    if unexpected:
        extras = ", ".join(unexpected)
        raise ConfigError(f"Unexpected table in [{label}]: {extras}")


def _optional_absolute_path_or_none(table: dict[str, Any], key: str) -> Path | None:
    if key not in table:
        return None
    return _require_absolute_path(table, key)


def _require_vnc_display_number(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"Missing or invalid non-negative integer value for {key!r}")
    if value > MAX_VNC_DISPLAY_NUMBER:
        raise ConfigError(
            f"{key!r} must be <= {MAX_VNC_DISPLAY_NUMBER} so the derived VNC port stays <= 65535"
        )
    return value


def _optional_port(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Missing or invalid positive integer value for {key!r}")
    if value > MAX_PORT_NUMBER:
        raise ConfigError(f"{key!r} must be <= {MAX_PORT_NUMBER}")
    return value


def _optional_command_or_absolute_path(table: dict[str, Any], key: str, default: str) -> str:
    value = _optional_non_empty_string(table, key, default=default).strip()
    path = Path(value)
    if path.is_absolute():
        return value
    if path.name != value:
        raise ConfigError(f"{key!r} must be a bare executable name or an absolute path")
    return value


def _require_moonlight_host(table: dict[str, Any], key: str) -> str:
    value = _require_non_empty_string(table, key).strip()
    if "://" in value or "/" in value:
        raise ConfigError(f"{key!r} must be a hostname or IP literal without a URL scheme")
    if value.startswith("[") or value.endswith("]"):
        raise ConfigError(f"{key!r} must not include IPv6 brackets")
    if any(character.isspace() for character in value):
        raise ConfigError(f"{key!r} must not contain whitespace")

    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass

    labels = value.split(".")
    if any(not label or not HOSTNAME_LABEL_RE.fullmatch(label) for label in labels):
        raise ConfigError(f"{key!r} must be a valid hostname or IP literal")
    return value


def _render_moonlight_host_authority(host: str, base_port: int) -> str:
    if base_port == DEFAULT_MOONLIGHT_BASE_PORT:
        return host

    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None

    if host_ip is not None and host_ip.version == 6:
        return f"[{host}]:{base_port}"
    return f"{host}:{base_port}"


def _optional_moonlight_resolution(table: dict[str, Any], key: str) -> str | None:
    if key not in table:
        return None

    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"Missing or invalid string value for {key!r}")

    normalized = value.strip()
    match = MOONLIGHT_RESOLUTION_RE.fullmatch(normalized)
    if match is None:
        raise ConfigError(f"{key!r} must be in '<width>x<height>' form")

    width = int(match.group("width"))
    height = int(match.group("height"))
    if width <= 0 or height <= 0:
        raise ConfigError(f"{key!r} width and height must be > 0")
    return f"{width}x{height}"


def _require_child_path(root: Path, child: Path, label: str) -> None:
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} must live under {root}") from exc
