from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2, copytree, ignore_patterns, rmtree, which
from typing import Callable, Sequence
import os
import pwd
import shlex
import subprocess
import sys
import textwrap


SERVICE_USER = "relayinner-display"
SERVICE_GROUP = "relayinner-display"
SERVICE_HOME = Path("/var/lib/relayinner-display")
SYSTEMD_RUNTIME_PATH = Path("/run/systemd/system")
NOLOGIN_FALLBACK = "/usr/sbin/nologin"
DEFAULT_STAGE_ROOT = Path("/")

REQUIRED_PACKAGES = (
    "python3",
    "python3-evdev",
    "cage",
    "seatd",
    "virt-viewer",
    "wlopm",
)
REQUIRED_SERVICES = (
    "relayinner-display-seatd.service",
    "relayinner-display-kiosk.service",
    "relayinner-displayd.service",
)
DEFAULT_PATH_ENV = "/usr/local/lib/relayinner-display:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEMD_START_LIMIT_INTERVAL_SEC = 120
SYSTEMD_START_LIMIT_BURST = 5


class BootstrapError(RuntimeError):
    """Raised when host bootstrap cannot be completed safely."""


@dataclass(frozen=True)
class HostInstallPaths:
    lib_dir: Path = Path("/usr/local/lib/relayinner-display")
    share_dir: Path = Path("/usr/local/share/relayinner-display")
    config_dir: Path = Path("/etc/relayinner-display")
    systemd_dir: Path = Path("/etc/systemd/system")
    logind_dir: Path = Path("/etc/systemd/logind.conf.d")
    service_home: Path = SERVICE_HOME

    @property
    def package_dir(self) -> Path:
        return self.lib_dir / "relayinner_display"

    @property
    def daemon_launcher(self) -> Path:
        return self.lib_dir / "relayinner-displayd"

    @property
    def session_launcher(self) -> Path:
        return self.lib_dir / "relayinner-display-session"

    @property
    def session_entrypoint_launcher(self) -> Path:
        return self.lib_dir / "session-entrypoint"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def logind_override_path(self) -> Path:
        return self.logind_dir / "relayinner-display.conf"

    @property
    def seatd_service_path(self) -> Path:
        return self.systemd_dir / REQUIRED_SERVICES[0]

    @property
    def kiosk_service_path(self) -> Path:
        return self.systemd_dir / REQUIRED_SERVICES[1]

    @property
    def daemon_service_path(self) -> Path:
        return self.systemd_dir / REQUIRED_SERVICES[2]


@dataclass(frozen=True)
class InstallResult:
    config_created: bool
    config_preserved: bool
    config_backup_path: Path | None
    manual_steps: tuple[str, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
OutputWriter = Callable[[str], None]


def render_sample_config() -> str:
    return textwrap.dedent(
        """\
        # RelayInnerDisplay sample config for Proxmox host direct install.
        # Edit vmid and node_name before starting the services for the first time.
        [target]
        vmid = 100
        node_name = "pve"
        guest_os = "windows"
        console_backend = "spice"

        [runtime]
        run_dir = "/run/relayinner-display"
        control_socket = "/run/relayinner-display/session.sock"
        spice_vv_path = "/run/relayinner-display/current.vv"
        log_namespace = "relayinner-display"

        [policy]
        poll_interval_ms = 2000
        reconnect_initial_ms = 1000
        reconnect_max_ms = 15000
        command_timeout_s = 10
        dpms_policy = "vm-power"
        dpms_off_delay_ms = 5000
        power_state_stabilize_ms = 3000
        power_button_action_when_running = "shutdown"
        power_button_action_when_stopped = "start"
        shutdown_timeout_s = 90

        [display]
        output_name = ""
        power_helper = "wlopm"

        [input]
        power_button_event = "/dev/input/by-path/platform-i8042-serio-0-event-power"
        forward_power_button = true
        debounce_ms = 2000
        """
    )


def render_launcher(module_name: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/python3
        from {module_name} import main

        raise SystemExit(main())
        """
    )


def render_logind_override() -> str:
    return "[Login]\nHandlePowerKey=ignore\n"


def build_installed_daemon_command(
    paths: HostInstallPaths = HostInstallPaths(),
    python_binary: str = "/usr/bin/python3",
) -> list[str]:
    return [python_binary, str(paths.daemon_launcher), "--config", str(paths.config_path)]


def build_installed_kiosk_command(
    paths: HostInstallPaths = HostInstallPaths(),
    cage_binary: str = "/usr/bin/cage",
) -> list[str]:
    return [cage_binary, "--", str(paths.session_entrypoint_launcher)]


def build_installed_seatd_command(
    seatd_binary: str = "/usr/bin/seatd",
    group_name: str = SERVICE_GROUP,
) -> list[str]:
    return [seatd_binary, "-g", group_name]


def render_daemon_service(paths: HostInstallPaths = HostInstallPaths()) -> str:
    exec_start = shlex.join(build_installed_daemon_command(paths))
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=RelayInnerDisplay Proxmox relay daemon
        After=local-fs.target network-online.target
        Wants=network-online.target
        StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}
        StartLimitBurst={SYSTEMD_START_LIMIT_BURST}

        [Service]
        Type=simple
        ExecStart={exec_start}
        Restart=always
        RestartSec=2

        [Install]
        WantedBy=multi-user.target
        """
    )


def render_kiosk_service(paths: HostInstallPaths = HostInstallPaths()) -> str:
    exec_start = shlex.join(build_installed_kiosk_command(paths))
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=RelayInnerDisplay Cage kiosk session on tty1
        After=systemd-user-sessions.service relayinner-display-seatd.service relayinner-displayd.service
        Requires=relayinner-display-seatd.service relayinner-displayd.service
        Conflicts=getty@tty1.service display-manager.service
        StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}
        StartLimitBurst={SYSTEMD_START_LIMIT_BURST}

        [Service]
        Type=simple
        User={SERVICE_USER}
        Group={SERVICE_GROUP}
        WorkingDirectory={paths.service_home}
        Environment=HOME={paths.service_home}
        Environment=PATH={DEFAULT_PATH_ENV}
        Environment=XDG_RUNTIME_DIR=/run/relayinner-display/user-runtime
        Environment=XDG_SESSION_TYPE=wayland
        Environment=SEATD_SOCK=/run/seatd.sock
        PermissionsStartOnly=true
        ExecStartPre=/usr/bin/install -d -o {SERVICE_USER} -g {SERVICE_GROUP} -m 0750 /run/relayinner-display
        ExecStartPre=/usr/bin/install -d -o {SERVICE_USER} -g {SERVICE_GROUP} -m 0700 /run/relayinner-display/user-runtime
        ExecStart={exec_start}
        Restart=always
        RestartSec=2
        StandardInput=tty
        TTYPath=/dev/tty1
        TTYReset=yes
        TTYVHangup=yes
        TTYVTDisallocate=yes
        NoNewPrivileges=true

        [Install]
        WantedBy=multi-user.target
        """
    )


def render_seatd_service() -> str:
    exec_start = shlex.join(build_installed_seatd_command())
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=RelayInnerDisplay seatd service
        After=systemd-udevd.service systemd-logind.service
        Before=relayinner-display-kiosk.service
        StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}
        StartLimitBurst={SYSTEMD_START_LIMIT_BURST}

        [Service]
        Type=simple
        ExecStart={exec_start}
        Restart=always
        RestartSec=2
        NoNewPrivileges=true

        [Install]
        WantedBy=multi-user.target
        """
    )


class HostBootstrapInstaller:
    def __init__(
        self,
        repo_root: Path,
        install_root: Path = DEFAULT_STAGE_ROOT,
        paths: HostInstallPaths = HostInstallPaths(),
        command_runner: CommandRunner = subprocess.run,
        output: OutputWriter | None = None,
        pveversion_finder: Callable[[str], str | None] = which,
        systemd_runtime_path: Path = SYSTEMD_RUNTIME_PATH,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.install_root = install_root.resolve()
        self.paths = paths
        self.command_runner = command_runner
        self.output = output or (lambda message: print(message))
        self.pveversion_finder = pveversion_finder
        self.systemd_runtime_path = systemd_runtime_path

    def install(
        self,
        *,
        skip_host_validation: bool = False,
        skip_package_install: bool = False,
        replace_config: bool = False,
    ) -> InstallResult:
        self._require_root()
        if not skip_host_validation:
            self.validate_host()

        self._verify_repo_layout()
        self.install_packages(skip_package_install=skip_package_install)
        self.ensure_service_user()
        self.install_runtime_tree()
        self.install_shared_assets()
        result = self.install_config(replace_config=replace_config)
        self.install_service_units()
        self.install_logind_override()
        self.configure_conflicting_units()
        self.daemon_reload()
        self.enable_services()
        self._print_manual_steps(result.manual_steps)
        return result

    def validate_host(self) -> None:
        if self.pveversion_finder("pveversion") is None:
            raise BootstrapError("Host validation failed: pveversion was not found; Proxmox VE is required")
        if not self.systemd_runtime_path.exists():
            raise BootstrapError(
                "Host validation failed: /run/systemd/system is missing; a systemd-based host is required"
            )

    def install_packages(self, *, skip_package_install: bool) -> None:
        if skip_package_install:
            self.output("Skipping apt package install (--skip-package-install)")
            return

        self.output("Installing required host packages with apt-get")
        self._run(["apt-get", "update"])
        self._run(["apt-get", "install", "-y", "--no-install-recommends", *REQUIRED_PACKAGES])

    def ensure_service_user(self) -> None:
        if self.install_root == DEFAULT_STAGE_ROOT and self._service_user_exists():
            self.output(f"System user {SERVICE_USER!r} already exists")
            return

        nologin_shell = which("nologin") or NOLOGIN_FALLBACK
        self.output(f"Ensuring system user {SERVICE_USER!r} exists")
        self._run(
            [
                "useradd",
                "--system",
                "--create-home",
                "--home-dir",
                str(self.paths.service_home),
                "--shell",
                nologin_shell,
                SERVICE_USER,
            ]
        )

    def install_runtime_tree(self) -> None:
        self.output(f"Installing runtime package into {self.paths.lib_dir}")
        lib_dir = self._stage(self.paths.lib_dir)
        lib_dir.mkdir(parents=True, exist_ok=True)

        package_dir = self._stage(self.paths.package_dir)
        if package_dir.exists():
            rmtree(package_dir)
        copytree(
            self.repo_root / "relayinner_display",
            package_dir,
            ignore=ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

        launchers = {
            self.paths.daemon_launcher: render_launcher("relayinner_display.daemon"),
            self.paths.session_launcher: render_launcher("relayinner_display.session"),
            self.paths.session_entrypoint_launcher: render_launcher("relayinner_display.kiosk"),
        }
        for host_path, content in launchers.items():
            self._write_text(self._stage(host_path), content, mode=0o755)

    def install_shared_assets(self) -> None:
        self.output(f"Installing static assets into {self.paths.share_dir}")
        share_dir = self._stage(self.paths.share_dir)
        share_dir.mkdir(parents=True, exist_ok=True)

        self._write_text(share_dir / "config.example.toml", render_sample_config(), mode=0o644)
        copy2(
            self.repo_root / "docs" / "proxmox-host-setup.md",
            share_dir / "proxmox-host-setup.md",
        )
        copy2(self.repo_root / "README.md", share_dir / "README.md")

    def install_config(self, *, replace_config: bool) -> InstallResult:
        config_dir = self._stage(self.paths.config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = self._stage(self.paths.config_path)
        config_created = False
        config_preserved = False
        config_backup_path: Path | None = None

        if config_path.exists() and not replace_config:
            config_preserved = True
            self.output(f"Preserving existing operator config at {self.paths.config_path}")
        else:
            if config_path.exists():
                config_backup_path = self._next_backup_path(config_path)
                copy2(config_path, config_backup_path)
                self.output(f"Backed up existing config to {config_backup_path}")
            self._write_text(config_path, render_sample_config(), mode=0o640)
            config_created = True
            self.output(f"Installed sample config to {self.paths.config_path}")

        return InstallResult(
            config_created=config_created,
            config_preserved=config_preserved,
            config_backup_path=config_backup_path,
            manual_steps=self.build_manual_steps(),
        )

    def install_service_units(self) -> None:
        self.output(f"Installing systemd units into {self.paths.systemd_dir}")
        self._write_text(self._stage(self.paths.seatd_service_path), render_seatd_service(), mode=0o644)
        self._write_text(
            self._stage(self.paths.kiosk_service_path),
            render_kiosk_service(self.paths),
            mode=0o644,
        )
        self._write_text(
            self._stage(self.paths.daemon_service_path),
            render_daemon_service(self.paths),
            mode=0o644,
        )

    def install_logind_override(self) -> None:
        self.output(f"Installing logind override into {self.paths.logind_override_path}")
        self._write_text(
            self._stage(self.paths.logind_override_path),
            render_logind_override(),
            mode=0o644,
        )

    def configure_conflicting_units(self) -> None:
        self.output("Masking conflicting tty1 services")
        self._run_optional(["systemctl", "disable", "--now", "getty@tty1.service"])
        self._run_optional(["systemctl", "mask", "getty@tty1.service"])
        if self._unit_exists("display-manager.service"):
            self._run_optional(["systemctl", "disable", "--now", "display-manager.service"])
            self._run_optional(["systemctl", "mask", "display-manager.service"])

    def daemon_reload(self) -> None:
        self.output("Reloading systemd")
        self._run(["systemctl", "daemon-reload"])

    def enable_services(self) -> None:
        self.output("Enabling relay services")
        self._run(["systemctl", "enable", *REQUIRED_SERVICES])

    def build_manual_steps(self) -> tuple[str, ...]:
        return (
            "Edit /etc/relayinner-display/config.toml and set [target].vmid and [target].node_name for the guest you want to relay.",
            "Review [input].power_button_event if the host exposes KEY_POWER on a different evdev path.",
            "After editing the config, run: systemctl restart relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service",
            "Reboot once to confirm tty1 comes back directly into the relay kiosk.",
        )

    def _service_user_exists(self) -> bool:
        try:
            pwd.getpwnam(SERVICE_USER)
        except KeyError:
            return False
        return True

    def _require_root(self) -> None:
        geteuid = getattr(os, "geteuid", None)
        if self.install_root != DEFAULT_STAGE_ROOT or geteuid is None:
            return
        if geteuid() != 0:
            raise BootstrapError("Installer must run as root")

    def _verify_repo_layout(self) -> None:
        required_paths = (
            self.repo_root / "relayinner_display",
            self.repo_root / "README.md",
            self.repo_root / "docs" / "proxmox-host-setup.md",
        )
        missing = [str(path) for path in required_paths if not path.exists()]
        if missing:
            joined = ", ".join(missing)
            raise BootstrapError(f"Repository layout is incomplete: missing {joined}")

    def _unit_exists(self, unit_name: str) -> bool:
        completed = self._run(
            ["systemctl", "list-unit-files", unit_name, "--no-legend", "--no-pager"],
            check=False,
        )
        return bool((completed.stdout or "").strip())

    def _stage(self, host_path: Path) -> Path:
        relative = host_path.relative_to("/")
        return self.install_root / relative

    def _write_text(self, path: Path, content: str, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    def _next_backup_path(self, config_path: Path) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return config_path.with_name(f"{config_path.name}.bak.{timestamp}")

    def _run(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = self.command_runner(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise BootstrapError(f"Command failed ({shlex.join(command)}){suffix}")
        return completed

    def _run_optional(self, command: Sequence[str]) -> None:
        completed = self._run(command, check=False)
        if completed.returncode == 0:
            self.output(f"Applied: {shlex.join(command)}")

    def _print_manual_steps(self, manual_steps: Sequence[str]) -> None:
        for step in manual_steps:
            self.output(f"MANUAL: {step}")


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="relayinner-display-bootstrap")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_STAGE_ROOT)
    parser.add_argument("--skip-package-install", action="store_true")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--skip-host-validation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installer = HostBootstrapInstaller(repo_root=args.repo_root, install_root=args.root)

    try:
        installer.install(
            skip_host_validation=args.skip_host_validation,
            skip_package_install=args.skip_package_install,
            replace_config=args.replace_config,
        )
    except BootstrapError as exc:
        print(f"relayinner-display-bootstrap: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
