from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Callable, Mapping
import os
import shlex
import sys

from .config import AppConfig, ConfigError, load_config
from .session import DEFAULT_CONFIG_PATH


DEFAULT_CAGE_BINARY = "cage"
DEFAULT_SWAY_BINARY = "sway"
DEFAULT_ENTRYPOINT_PATH = "/usr/local/lib/relayinner-display/session-entrypoint"
DEFAULT_SWAY_CONFIG_PATH = Path("/run/relayinner-display/sway.config")
DEFAULT_SYS_CLASS_DRM_PATH = Path("/sys/class/drm")
WLR_DRM_NO_ATOMIC_ENV = "WLR_DRM_NO_ATOMIC"
WLR_DRM_NO_MODIFIERS_ENV = "WLR_DRM_NO_MODIFIERS"
WLR_DRM_COMPATIBILITY_ENV_NAMES = (
    WLR_DRM_NO_ATOMIC_ENV,
    WLR_DRM_NO_MODIFIERS_ENV,
)


ExecFunction = Callable[[str, list[str], dict[str, str]], None]


def build_cage_command(
    entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH,
    cage_binary: str = DEFAULT_CAGE_BINARY,
) -> list[str]:
    return [cage_binary, "--", entrypoint_path]


def build_sway_command(
    sway_config_path: Path = DEFAULT_SWAY_CONFIG_PATH,
    sway_binary: str = DEFAULT_SWAY_BINARY,
) -> list[str]:
    return [sway_binary, "--config", str(sway_config_path)]


def build_kiosk_command(
    config: AppConfig,
    *,
    entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH,
    cage_binary: str = DEFAULT_CAGE_BINARY,
    sway_binary: str = DEFAULT_SWAY_BINARY,
    sway_config_path: Path = DEFAULT_SWAY_CONFIG_PATH,
) -> list[str]:
    if config.kiosk.resolved_compositor == "sway":
        return build_sway_command(sway_config_path=sway_config_path, sway_binary=sway_binary)
    return build_cage_command(entrypoint_path=entrypoint_path, cage_binary=cage_binary)


def render_sway_config(
    config_path: Path,
    *,
    entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH,
    output_name: str = "",
) -> str:
    lines = [
        "default_border pixel 0",
        "focus_follows_mouse no",
    ]
    if output_name:
        lines.append(f"workspace 1 output {output_name}")
    lines.append(
        f"exec {shlex.join([entrypoint_path, '--config', str(config_path)])}"
    )
    return "\n".join(lines) + "\n"


def detect_connected_drm_outputs(
    sys_class_drm_path: Path = DEFAULT_SYS_CLASS_DRM_PATH,
) -> set[str] | None:
    status_paths = sorted(sys_class_drm_path.glob("card*-*/status"))
    if not status_paths:
        return None

    outputs: set[str] = set()
    for status_path in status_paths:
        try:
            status = status_path.read_text(encoding="utf-8").strip().casefold()
        except OSError:
            continue

        if status != "connected":
            continue

        connector_name = status_path.parent.name.partition("-")[2]
        if connector_name:
            outputs.add(connector_name)

    return outputs


def resolve_sway_output_name(
    output_name: str,
    connected_outputs: set[str] | None,
) -> tuple[str, str | None]:
    requested_output = output_name.strip()
    if not requested_output:
        return "", None
    if connected_outputs is None or requested_output in connected_outputs:
        return requested_output, None
    return (
        "",
        "requested output pin for "
        f"{requested_output} is unavailable at sway startup; continuing without workspace pin",
    )


def write_sway_config(
    config: AppConfig,
    config_path: Path,
    *,
    sway_config_path: Path = DEFAULT_SWAY_CONFIG_PATH,
    entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH,
    connected_outputs: set[str] | None = None,
) -> tuple[Path, str | None]:
    output_name, output_warning = resolve_sway_output_name(
        config.display.output_name,
        connected_outputs,
    )
    sway_config_path.parent.mkdir(parents=True, exist_ok=True)
    sway_config_path.write_text(
        render_sway_config(
            config_path,
            entrypoint_path=entrypoint_path,
            output_name=output_name,
        ),
        encoding="utf-8",
    )
    os.chmod(sway_config_path, 0o600)
    return sway_config_path, output_warning


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="relayinner-display-kiosk")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--entrypoint-path", default=DEFAULT_ENTRYPOINT_PATH)
    parser.add_argument("--cage-binary", default=DEFAULT_CAGE_BINARY)
    parser.add_argument("--sway-binary", default=DEFAULT_SWAY_BINARY)
    parser.add_argument("--sway-config-path", type=Path, default=DEFAULT_SWAY_CONFIG_PATH)
    return parser


def _build_exec_env(source_env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source_env is None else source_env
    return dict(source)


def build_drm_compatibility_env(drm_compatibility: str) -> dict[str, str]:
    if drm_compatibility == "auto":
        return {WLR_DRM_NO_MODIFIERS_ENV: "1"}
    if drm_compatibility == "legacy-drm":
        return {
            WLR_DRM_NO_ATOMIC_ENV: "1",
            WLR_DRM_NO_MODIFIERS_ENV: "1",
        }
    return {}


def format_drm_compatibility_env(env: Mapping[str, str]) -> str:
    if not env:
        return "none"
    return ",".join(f"{name}={env[name]}" for name in sorted(env))


def build_kiosk_env(
    config: AppConfig,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = _build_exec_env(source_env)
    for name in WLR_DRM_COMPATIBILITY_ENV_NAMES:
        env.pop(name, None)
    env.update(build_drm_compatibility_env(config.display.drm_compatibility))
    return env


def main(argv: list[str] | None = None, execvpe: ExecFunction = os.execvpe) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"relayinner-display-kiosk: invalid config: {exc}", file=sys.stderr)
        return 78

    print(
        "relayinner-display-kiosk: "
        f"backend={config.target.console_backend} "
        f"configured_kiosk_compositor={config.kiosk.compositor} "
        f"kiosk_compositor={config.kiosk.resolved_compositor}"
    )
    drm_env = build_drm_compatibility_env(config.display.drm_compatibility)
    print(
        "relayinner-display-kiosk: "
        f"display_drm_compatibility={config.display.drm_compatibility} "
        f"effective_wlroots_drm_env={format_drm_compatibility_env(drm_env)}"
    )

    if config.kiosk.resolved_compositor == "sway":
        connected_outputs = None
        if config.display.output_name:
            connected_outputs = detect_connected_drm_outputs()
        sway_config_path, output_warning = write_sway_config(
            config,
            args.config,
            sway_config_path=args.sway_config_path,
            entrypoint_path=args.entrypoint_path,
            connected_outputs=connected_outputs,
        )
        print(f"relayinner-display-kiosk: wrote {sway_config_path}")
        if config.display.output_name:
            print(
                "relayinner-display-kiosk: "
                f"requested output pin for workspace 1 on {config.display.output_name}"
            )
        if output_warning is not None:
            print(f"relayinner-display-kiosk: WARNING: {output_warning}")

    command = build_kiosk_command(
        config,
        entrypoint_path=args.entrypoint_path,
        cage_binary=args.cage_binary,
        sway_binary=args.sway_binary,
        sway_config_path=args.sway_config_path,
    )

    try:
        execvpe(command[0], command, build_kiosk_env(config))
    except OSError as exc:
        print(
            f"relayinner-display-kiosk: failed to exec {command[0]}: {exc}",
            file=sys.stderr,
        )
        return 127

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
