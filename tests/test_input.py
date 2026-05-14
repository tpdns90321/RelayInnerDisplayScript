from __future__ import annotations

import builtins
from pathlib import Path
from tempfile import TemporaryDirectory
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


if __name__ == "__main__":
    unittest.main()
