from __future__ import annotations

import builtins
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

from relayinner_display.input import EvdevPowerButtonSource, LogindPowerButtonPolicyChecker, PowerButtonError


class EvdevPowerButtonSourceTests(unittest.TestCase):
    def test_unopened_source_reports_no_presses(self) -> None:
        source = EvdevPowerButtonSource("/dev/input/event0")

        self.assertEqual(source.device_path, Path("/dev/input/event0"))
        self.assertEqual(source.poll_presses(), 0)

    def test_open_requires_python_evdev_dependency(self) -> None:
        source = EvdevPowerButtonSource("/dev/input/event0")
        real_import = builtins.__import__

        def reject_evdev(name: str, *args: object, **kwargs: object) -> object:
            if name == "evdev":
                raise ImportError("no evdev")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=reject_evdev),
            self.assertRaisesRegex(PowerButtonError, "python-evdev is required"),
        ):
            source.open()

    def test_open_reports_device_open_errors(self) -> None:
        class FailingInputDevice:
            def __init__(self, path: str) -> None:
                raise OSError("permission denied")

        fake_evdev = types.SimpleNamespace(
            InputDevice=FailingInputDevice,
            ecodes=types.SimpleNamespace(EV_KEY=1, KEY_POWER=116),
        )
        source = EvdevPowerButtonSource("/dev/input/event0")

        with (
            patch.dict(sys.modules, {"evdev": fake_evdev}),
            self.assertRaisesRegex(
                PowerButtonError,
                "Unable to open power-button device /dev/input/event0: permission denied",
            ),
        ):
            source.open()

    def test_open_poll_and_close_counts_power_key_presses(self) -> None:
        events = [
            types.SimpleNamespace(type=1, code=116, value=0),
            types.SimpleNamespace(type=1, code=116, value=1),
            types.SimpleNamespace(type=1, code=30, value=1),
            types.SimpleNamespace(type=2, code=116, value=1),
            types.SimpleNamespace(type=1, code=116, value=1),
        ]
        opened_paths: list[str] = []

        class FakeInputDevice:
            def __init__(self, path: str) -> None:
                opened_paths.append(path)
                self.closed = False

            def read_one(self) -> object | None:
                if events:
                    return events.pop(0)
                return None

            def close(self) -> None:
                self.closed = True

        fake_evdev = types.SimpleNamespace(
            InputDevice=FakeInputDevice,
            ecodes=types.SimpleNamespace(EV_KEY=1, KEY_POWER=116),
        )
        source = EvdevPowerButtonSource("/dev/input/event0")

        with patch.dict(sys.modules, {"evdev": fake_evdev}):
            source.open()
            device = source._device
            presses = source.poll_presses()
            source.close()

        self.assertEqual(opened_paths, ["/dev/input/event0"])
        self.assertEqual(presses, 2)
        self.assertTrue(device.closed)
        self.assertEqual(source.poll_presses(), 0)


class LogindPowerButtonPolicyCheckerTests(unittest.TestCase):
    def test_validate_accepts_ignore_from_dropin(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main_config = root / "logind.conf"
            dropin_dir = root / "logind.conf.d"
            dropin_dir.mkdir()
            main_config.write_text("[Login]\nHandlePowerKey=poweroff\n", encoding="utf-8")
            (dropin_dir / "90-relay.conf").write_text(
                "[Login]\nHandlePowerKey=ignore\n",
                encoding="utf-8",
            )

            checker = LogindPowerButtonPolicyChecker(
                main_configs=(main_config,),
                dropin_dirs=(dropin_dir,),
            )

            checker.validate()

    def test_validate_rejects_non_ignore_policy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "logind.conf"
            config_path.write_text("[Login]\nHandlePowerKey=poweroff\n", encoding="utf-8")

            checker = LogindPowerButtonPolicyChecker(
                main_configs=(config_path,),
                dropin_dirs=(),
            )

            with self.assertRaises(PowerButtonError):
                checker.validate()

    def test_validate_ignores_irrelevant_logind_lines_and_missing_dropins(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "logind.conf"
            missing_dropin_dir = root / "missing-dropins"
            config_path.write_text(
                "\n"
                "# global comment\n"
                "HandlePowerKey=poweroff\n"
                "[Service]\n"
                "HandlePowerKey=poweroff\n"
                "NoEqualsHere\n"
                "[Login]\n"
                "NoEqualsHere\n"
                "HandlePowerKey=ignore ; local comment\n",
                encoding="utf-8",
            )

            checker = LogindPowerButtonPolicyChecker(
                main_configs=(config_path,),
                dropin_dirs=(missing_dropin_dir,),
            )

            checker.validate()


if __name__ == "__main__":
    unittest.main()
