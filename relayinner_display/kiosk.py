from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Callable
import os
import sys

from .session import DEFAULT_CONFIG_PATH, build_session_env


DEFAULT_SESSION_BINARY = "/usr/local/lib/relayinner-display/relayinner-display-session"
DEFAULT_ENTRYPOINT_PATH = "/usr/local/lib/relayinner-display/session-entrypoint"


ExecFunction = Callable[[str, list[str], dict[str, str]], None]


def build_session_entrypoint_command(
    config_path: Path,
    session_binary: str = DEFAULT_SESSION_BINARY,
) -> list[str]:
    return [session_binary, "--config", str(config_path)]


def build_kiosk_service_command(entrypoint_path: str = DEFAULT_ENTRYPOINT_PATH) -> list[str]:
    return ["cage", "--", entrypoint_path]


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="relayinner-display-session-entrypoint")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--session-binary", default=DEFAULT_SESSION_BINARY)
    return parser


def main(argv: list[str] | None = None, execvpe: ExecFunction = os.execvpe) -> int:
    args = build_parser().parse_args(argv)
    command = build_session_entrypoint_command(args.config, args.session_binary)

    try:
        execvpe(command[0], command, build_session_env())
    except OSError as exc:
        print(
            f"relayinner-display-session-entrypoint: failed to exec {command[0]}: {exc}",
            file=sys.stderr,
        )
        return 127

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
