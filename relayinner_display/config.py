from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


SUPPORTED_CONSOLE_BACKENDS = {"spice"}
SUPPORTED_DPMS_POLICIES = {"vm-power"}
SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_RUNNING = {"shutdown"}
SUPPORTED_POWER_BUTTON_ACTIONS_WHEN_STOPPED = {"start"}
DEFAULT_POWER_BUTTON_EVENT = Path("/dev/input/by-path/platform-i8042-serio-0-event-power")
DEFAULT_RUNTIME_RUN_DIR = Path("/run/relayinner-display")
DEFAULT_CONTROL_SOCKET = DEFAULT_RUNTIME_RUN_DIR / "session.sock"
DEFAULT_SPICE_VV_PATH = DEFAULT_RUNTIME_RUN_DIR / "current.vv"
DEFAULT_LOG_NAMESPACE = "relayinner-display"


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
class AppConfig:
    target: TargetConfig
    runtime: RuntimeConfig
    policy: PolicyConfig
    display: DisplayConfig = field(default_factory=DisplayConfig)
    input: InputConfig = field(default_factory=InputConfig)


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
    input_table = _optional_table(raw, "input")

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

    runtime = RuntimeConfig(
        run_dir=_require_absolute_path(runtime_table, "run_dir"),
        control_socket=_require_absolute_path(runtime_table, "control_socket"),
        spice_vv_path=_require_absolute_path(runtime_table, "spice_vv_path"),
        log_namespace=_require_non_empty_string(runtime_table, "log_namespace"),
    )
    _require_child_path(runtime.run_dir, runtime.control_socket, "runtime.control_socket")
    _require_child_path(runtime.run_dir, runtime.spice_vv_path, "runtime.spice_vv_path")

    display = DisplayConfig(
        output_name=_optional_string(display_table, "output_name", default=""),
        power_helper=_optional_non_empty_string(display_table, "power_helper", default="wlr-randr"),
    )

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
        input=input_config,
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


def _require_child_path(root: Path, child: Path, label: str) -> None:
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} must live under {root}") from exc
