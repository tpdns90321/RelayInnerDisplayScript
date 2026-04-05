#!/usr/bin/env python3
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ
from typing import Iterable
import signal
import sys
import time


DEFAULT_INPUT_DIR = Path("/dev/input")
TARGET_KEYS = {"KEY_POWER", "KEY_SLEEP", "KEY_WAKEUP"}
KEY_VALUES = {
    0: "release",
    1: "press",
    2: "repeat",
}


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description=(
            "Open every evdev-style event entry in a directory and print key events. "
            "Use this to discover which host input path emits KEY_POWER."
        )
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing event entries (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--all-keys",
        action="store_true",
        help="Print every EV_KEY event instead of only KEY_POWER/KEY_SLEEP/KEY_WAKEUP",
    )
    return parser


def iter_event_paths(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")

    return sorted(
        path
        for path in input_dir.iterdir()
        if "event" in path.name
    )


def open_devices(paths: Iterable[Path]):
    try:
        from evdev import InputDevice
    except ImportError as exc:
        raise RuntimeError("python-evdev is required; install evdev first") from exc

    opened = []
    errors: list[tuple[Path, str]] = []
    for path in paths:
        try:
            device = InputDevice(str(path))
        except OSError as exc:
            errors.append((path, str(exc)))
            continue
        opened.append((path, device))

    return opened, errors


def describe_key_event(event, ecodes) -> tuple[str, str]:
    key_name = ecodes.bytype[ecodes.EV_KEY].get(event.code, f"KEY_{event.code}")
    value_name = KEY_VALUES.get(event.value, str(event.value))
    return key_name, value_name


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        paths = iter_event_paths(args.dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not paths:
        print(f"No event entries found under {args.dir}", file=sys.stderr)
        return 1

    try:
        opened, errors = open_devices(paths)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for path, reason in errors:
        print(f"[skip] {path} ({reason})", file=sys.stderr)

    if not opened:
        print("No input devices could be opened", file=sys.stderr)
        return 1

    from evdev import ecodes

    print(f"Watching input devices under {args.dir}:")
    for path, device in opened:
        print(f"  {path} -> {device.path} ({device.name})")
    print()
    if args.all_keys:
        print("Filtering: all EV_KEY events")
    else:
        print(f"Filtering: {', '.join(sorted(TARGET_KEYS))}")
    print("Press Ctrl+C to stop.", flush=True)

    selector = DefaultSelector()
    running = True

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        for path, device in opened:
            selector.register(device, EVENT_READ, data=path)

        while running:
            ready = selector.select(timeout=0.5)
            if not ready:
                continue

            for key, _ in ready:
                device = key.fileobj
                path = key.data
                for event in device.read():
                    if event.type != ecodes.EV_KEY:
                        continue
                    key_name, value_name = describe_key_event(event, ecodes)
                    if not args.all_keys and key_name not in TARGET_KEYS:
                        continue
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"{timestamp} {path.name} -> {device.name}: {key_name} {value_name}",
                        flush=True,
                    )
    finally:
        for _, device in opened:
            device.close()
        selector.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
