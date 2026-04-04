from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from relayinner_display.input import LogindPowerButtonPolicyChecker, PowerButtonError


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
