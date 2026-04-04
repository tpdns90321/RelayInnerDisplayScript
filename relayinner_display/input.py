from __future__ import annotations

from pathlib import Path


DEFAULT_LOGIND_MAIN_CONFIGS = (
    Path("/usr/lib/systemd/logind.conf"),
    Path("/usr/local/lib/systemd/logind.conf"),
    Path("/run/systemd/logind.conf"),
    Path("/etc/systemd/logind.conf"),
)
DEFAULT_LOGIND_DROPIN_DIRS = (
    Path("/usr/lib/systemd/logind.conf.d"),
    Path("/usr/local/lib/systemd/logind.conf.d"),
    Path("/run/systemd/logind.conf.d"),
    Path("/etc/systemd/logind.conf.d"),
)


class PowerButtonError(RuntimeError):
    """Raised when the power-button capture path cannot be initialized safely."""


class EvdevPowerButtonSource:
    def __init__(self, device_path: Path | str) -> None:
        self.device_path = Path(device_path)
        self._device = None
        self._ecodes = None

    def open(self) -> None:
        try:
            from evdev import InputDevice, ecodes
        except ImportError as exc:
            raise PowerButtonError("python-evdev is required for power-button forwarding") from exc

        try:
            self._device = InputDevice(str(self.device_path))
        except OSError as exc:
            raise PowerButtonError(f"Unable to open power-button device {self.device_path}: {exc}") from exc

        self._ecodes = ecodes

    def poll_presses(self) -> int:
        if self._device is None or self._ecodes is None:
            return 0

        accepted = 0
        while True:
            event = self._device.read_one()
            if event is None:
                return accepted
            if (
                event.type == self._ecodes.EV_KEY
                and event.code == self._ecodes.KEY_POWER
                and event.value == 1
            ):
                accepted += 1

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            finally:
                self._device = None
                self._ecodes = None


class LogindPowerButtonPolicyChecker:
    def __init__(
        self,
        main_configs: tuple[Path, ...] = DEFAULT_LOGIND_MAIN_CONFIGS,
        dropin_dirs: tuple[Path, ...] = DEFAULT_LOGIND_DROPIN_DIRS,
    ) -> None:
        self.main_configs = main_configs
        self.dropin_dirs = dropin_dirs

    def validate(self) -> None:
        effective_value = self._effective_handle_power_key()
        if effective_value != "ignore":
            value = effective_value or "<unset>"
            raise PowerButtonError(
                "Host power-button handling is not disabled; "
                f"effective HandlePowerKey={value!r}"
            )

    def _effective_handle_power_key(self) -> str | None:
        effective_value: str | None = None
        for path in self._iter_config_paths():
            parsed = self._read_handle_power_key(path)
            if parsed is not None:
                effective_value = parsed
        return effective_value

    def _iter_config_paths(self) -> list[Path]:
        paths: list[Path] = []
        for path in self.main_configs:
            if path.is_file():
                paths.append(path)
        for directory in self.dropin_dirs:
            if not directory.is_dir():
                continue
            paths.extend(sorted(path for path in directory.glob("*.conf") if path.is_file()))
        return paths

    def _read_handle_power_key(self, path: Path) -> str | None:
        in_login_section = False
        value: str | None = None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                in_login_section = line == "[Login]"
                continue
            if not in_login_section or "=" not in line:
                continue
            key, candidate = line.split("=", 1)
            if key.strip() == "HandlePowerKey":
                value = candidate.strip()
        return value
