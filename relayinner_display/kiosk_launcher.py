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


def write_sway_config(
    config: AppConfig,
    config_path: Path,
    *,
    sway_config_path: Path = DEFAULT_SWAY_CONFIG_PATH,
    entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH,
) -> Path:
    sway_config_path.parent.mkdir(parents=True, exist_ok=True)
    sway_config_path.write_text(
        render_sway_config(
            config_path,
            entrypoint_path=entrypoint_path,
            output_name=config.display.output_name,
        ),
        encoding="utf-8",
    )
    os.chmod(sway_config_path, 0o600)
    return sway_config_path


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

    if config.kiosk.resolved_compositor == "sway":
        sway_config_path = write_sway_config(
            config,
            args.config,
            sway_config_path=args.sway_config_path,
            entrypoint_path=args.entrypoint_path,
        )
        print(f"relayinner-display-kiosk: wrote {sway_config_path}")
        if config.display.output_name:
            print(
                "relayinner-display-kiosk: "
                f"requested output pin for workspace 1 on {config.display.output_name}"
            )

    command = build_kiosk_command(
        config,
        entrypoint_path=args.entrypoint_path,
        cage_binary=args.cage_binary,
        sway_binary=args.sway_binary,
        sway_config_path=args.sway_config_path,
    )

    try:
        execvpe(command[0], command, _build_exec_env())
    except OSError as exc:
        print(
            f"relayinner-display-kiosk: failed to exec {command[0]}: {exc}",
            file=sys.stderr,
        )
        return 127

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
