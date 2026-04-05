from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2, copytree, ignore_patterns, rmtree, which
from typing import Callable, Sequence
import grp
import json
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
CONFLICTING_UNITS = (
    "getty@tty1.service",
    "display-manager.service",
)
DEFAULT_PATH_ENV = "/usr/local/lib/relayinner-display:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEMD_START_LIMIT_INTERVAL_SEC = 120
SYSTEMD_START_LIMIT_BURST = 5
INSTALL_STATE_SCHEMA_VERSION = 1
RUNTIME_STATE_DIR = Path("/run/relayinner-display")


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

    @property
    def systemd_unit_paths(self) -> tuple[Path, ...]:
        return (
            self.seatd_service_path,
            self.kiosk_service_path,
            self.daemon_service_path,
        )

    @property
    def install_state_path(self) -> Path:
        return self.service_home / "install-state.json"


@dataclass(frozen=True)
class InstallResult:
    config_action: str
    config_backup_path: Path | None
    manual_steps: tuple[str, ...]

    @property
    def config_created(self) -> bool:
        return self.config_action in {"created", "replaced"}

    @property
    def config_preserved(self) -> bool:
        return self.config_action == "preserved"


@dataclass(frozen=True)
class ConflictingUnitState:
    existed: bool
    enabled_before: bool
    active_before: bool
    masked_before: bool
    changed_by_installer: bool


@dataclass(frozen=True)
class UninstallState:
    install_state_present: bool
    lib_dir: Path
    share_dir: Path
    config_dir: Path
    config_path: Path
    logind_override_path: Path
    service_home: Path
    systemd_unit_paths: tuple[Path, ...]
    service_user_created: bool
    getty_state: ConflictingUnitState | None
    display_manager_state: ConflictingUnitState | None


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
OutputWriter = Callable[[str], None]
TimestampProvider = Callable[[], datetime]
ServiceUserExistsChecker = Callable[[str], bool]
GroupDetector = Callable[[], tuple[str, ...]]


def resolve_host_binary(binary_name: str, default_path: str) -> str:
    resolved = which(binary_name, path=DEFAULT_PATH_ENV)
    return resolved or default_path


def detect_drm_device_groups(device_dir: Path = Path("/dev/dri")) -> tuple[str, ...]:
    groups: list[str] = []
    seen: set[str] = set()
    device_nodes = sorted(device_dir.glob("card*")) + sorted(device_dir.glob("renderD*"))

    for device_node in device_nodes:
        try:
            device_gid = device_node.stat().st_gid
        except OSError:
            continue

        try:
            group_name = grp.getgrgid(device_gid).gr_name
        except KeyError:
            group_name = str(device_gid)

        if group_name in seen:
            continue

        seen.add(group_name)
        groups.append(group_name)

    return tuple(groups)


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
    cage_binary: str | None = None,
) -> list[str]:
    cage_binary = cage_binary or resolve_host_binary("cage", "/usr/bin/cage")
    return [cage_binary, "--", str(paths.session_entrypoint_launcher)]


def build_installed_seatd_command(
    seatd_binary: str | None = None,
    group_name: str = SERVICE_GROUP,
) -> list[str]:
    seatd_binary = seatd_binary or resolve_host_binary("seatd", "/usr/bin/seatd")
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


def render_kiosk_service(
    paths: HostInstallPaths = HostInstallPaths(),
    supplementary_groups: Sequence[str] = (),
) -> str:
    exec_start = shlex.join(build_installed_kiosk_command(paths))
    supplementary_groups_line = ""
    if supplementary_groups:
        supplementary_groups_line = f"SupplementaryGroups={' '.join(supplementary_groups)}\n"
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
        {supplementary_groups_line}\
        WorkingDirectory={paths.service_home}
        Environment=HOME={paths.service_home}
        Environment=PATH={DEFAULT_PATH_ENV}
        Environment=XDG_RUNTIME_DIR=/run/relayinner-display/user-runtime
        Environment=XDG_SESSION_TYPE=wayland
        Environment=LIBSEAT_BACKEND=seatd
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
        now_provider: TimestampProvider | None = None,
        service_user_exists_checker: ServiceUserExistsChecker | None = None,
        kiosk_supplementary_groups_detector: GroupDetector | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.install_root = install_root.resolve()
        self.paths = paths
        self.command_runner = command_runner
        self.output = output or (lambda message: print(message))
        self.pveversion_finder = pveversion_finder
        self.systemd_runtime_path = systemd_runtime_path
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.service_user_exists_checker = service_user_exists_checker or self._lookup_service_user
        if kiosk_supplementary_groups_detector is not None:
            self.kiosk_supplementary_groups_detector = kiosk_supplementary_groups_detector
        elif self.install_root == DEFAULT_STAGE_ROOT:
            self.kiosk_supplementary_groups_detector = detect_drm_device_groups
        else:
            self.kiosk_supplementary_groups_detector = lambda: ()

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
        service_user_created = self.ensure_service_user()
        self.install_runtime_tree()
        self.install_shared_assets()
        result = self.install_config(replace_config=replace_config)
        self.install_service_units()
        self.install_logind_override()
        conflicting_units = self.configure_conflicting_units()
        self.daemon_reload()
        self.enable_services()
        self.write_install_state(
            result=result,
            service_user_created=service_user_created,
            conflicting_units=conflicting_units,
        )
        self._print_manual_steps(result.manual_steps)
        return result

    def validate_host(self) -> None:
        if self.pveversion_finder("pveversion") is None:
            raise BootstrapError("Host validation failed: pveversion was not found; Proxmox VE is required")
        if not self.systemd_runtime_path.exists():
            raise BootstrapError(
                "Host validation failed: /run/systemd/system is missing; a systemd-based host is required"
            )

    def uninstall(self, *, purge_config: bool = False) -> None:
        self._require_root()
        self._verify_repo_layout()
        uninstall_state = self._load_uninstall_state()

        if not uninstall_state.install_state_present:
            self.output(
                f"WARNING: install-state missing at {self.paths.install_state_path}; using best-effort uninstall with reduced restore precision"
            )

        self.stop_services()
        self.disable_services()
        self.remove_service_units(uninstall_state.systemd_unit_paths)
        self.remove_logind_override(uninstall_state.logind_override_path)
        self.daemon_reload()
        self.restore_getty(uninstall_state.getty_state)
        self.restore_display_manager(uninstall_state.display_manager_state)
        self.remove_runtime_tree(uninstall_state.lib_dir)
        self.remove_shared_assets(uninstall_state.share_dir)
        self.remove_runtime_state_dir()
        self.remove_service_user(uninstall_state)
        if purge_config:
            self.purge_config(uninstall_state.config_path, uninstall_state.config_dir)
        else:
            self.output(f"Preserving operator config at {uninstall_state.config_path}")
        self.remove_install_state()
        self.remove_service_home(uninstall_state)

    def install_packages(self, *, skip_package_install: bool) -> None:
        if skip_package_install:
            self.output("Skipping apt package install (--skip-package-install)")
            return

        self.output("Installing required host packages with apt-get")
        self._run(["apt-get", "update"])
        self._run(["apt-get", "install", "-y", "--no-install-recommends", *REQUIRED_PACKAGES])

    def ensure_service_user(self) -> bool:
        if self._service_user_exists():
            self.output(f"System user {SERVICE_USER!r} already exists")
            return False

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
        return True

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
        config_backup_path: Path | None = None
        config_action = "created"

        if config_path.exists() and not replace_config:
            config_action = "preserved"
            self.output(f"Preserving existing operator config at {self.paths.config_path}")
        else:
            if config_path.exists():
                config_action = "replaced"
                config_backup_path = self._next_backup_path(config_path)
                copy2(config_path, config_backup_path)
                self.output(f"Backed up existing config to {config_backup_path}")
            self._write_text(config_path, render_sample_config(), mode=0o640)
            self.output(f"Installed sample config to {self.paths.config_path}")

        return InstallResult(
            config_action=config_action,
            config_backup_path=config_backup_path,
            manual_steps=self.build_manual_steps(),
        )

    def install_service_units(self) -> None:
        self.output(f"Installing systemd units into {self.paths.systemd_dir}")
        kiosk_supplementary_groups = tuple(dict.fromkeys(self.kiosk_supplementary_groups_detector()))
        if kiosk_supplementary_groups:
            self.output("Detected kiosk DRM access groups: " + ", ".join(kiosk_supplementary_groups))
        else:
            self.output("WARNING: no DRM access groups detected under /dev/dri; Cage may fail to access the GPU")
        self._write_text(self._stage(self.paths.seatd_service_path), render_seatd_service(), mode=0o644)
        self._write_text(
            self._stage(self.paths.kiosk_service_path),
            render_kiosk_service(self.paths, supplementary_groups=kiosk_supplementary_groups),
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

    def configure_conflicting_units(self) -> dict[str, ConflictingUnitState]:
        self.output("Applying conflicting unit policy")
        conflicting_units: dict[str, ConflictingUnitState] = {}
        for unit_name in CONFLICTING_UNITS:
            initial_state = self._capture_conflicting_unit_state(unit_name)
            changed_by_installer = self._configure_conflicting_unit(unit_name, initial_state)
            conflicting_units[unit_name] = ConflictingUnitState(
                existed=initial_state.existed,
                enabled_before=initial_state.enabled_before,
                active_before=initial_state.active_before,
                masked_before=initial_state.masked_before,
                changed_by_installer=changed_by_installer,
            )
        return conflicting_units

    def daemon_reload(self) -> None:
        self.output("Reloading systemd")
        self._run(["systemctl", "daemon-reload"])

    def enable_services(self) -> None:
        self.output("Enabling relay services")
        self._run(["systemctl", "enable", *REQUIRED_SERVICES])

    def stop_services(self) -> None:
        self.output("Stopping relay services")
        for service_name in REQUIRED_SERVICES:
            self._run_optional(["systemctl", "stop", service_name])

    def disable_services(self) -> None:
        self.output("Disabling relay services")
        for service_name in REQUIRED_SERVICES:
            self._run_optional(["systemctl", "disable", service_name])

    def write_install_state(
        self,
        *,
        result: InstallResult,
        service_user_created: bool,
        conflicting_units: dict[str, ConflictingUnitState],
    ) -> None:
        install_state = {
            "schema_version": INSTALL_STATE_SCHEMA_VERSION,
            "installed_at": self._format_timestamp(self.now_provider()),
            "managed_paths": {
                "lib_dir": str(self.paths.lib_dir),
                "share_dir": str(self.paths.share_dir),
                "config_dir": str(self.paths.config_dir),
                "config_path": str(self.paths.config_path),
                "logind_override_path": str(self.paths.logind_override_path),
                "service_home": str(self.paths.service_home),
                "systemd_units": [str(path) for path in self.paths.systemd_unit_paths],
            },
            "config_state": {
                "action": result.config_action,
                "backup_path": str(result.config_backup_path) if result.config_backup_path is not None else None,
            },
            "service_user": {
                "name": SERVICE_USER,
                "created_by_installer": service_user_created,
            },
            "conflicting_units": {
                unit_name: {
                    "existed": state.existed,
                    "enabled_before": state.enabled_before,
                    "active_before": state.active_before,
                    "masked_before": state.masked_before,
                    "changed_by_installer": state.changed_by_installer,
                }
                for unit_name, state in conflicting_units.items()
            },
        }
        self.output(f"Writing install-state record to {self.paths.install_state_path}")
        self._write_text(
            self._stage(self.paths.install_state_path),
            json.dumps(install_state, indent=2) + "\n",
            mode=0o640,
        )

    def build_manual_steps(self) -> tuple[str, ...]:
        return (
            "Edit /etc/relayinner-display/config.toml and set [target].vmid and [target].node_name for the guest you want to relay.",
            "Review [input].power_button_event if the host exposes KEY_POWER on a different evdev path.",
            "After editing the config, run: systemctl restart relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service",
            "Reboot once to confirm tty1 comes back directly into the relay kiosk.",
        )

    def _service_user_exists(self) -> bool:
        return self.service_user_exists_checker(SERVICE_USER)

    def _lookup_service_user(self, user_name: str) -> bool:
        try:
            pwd.getpwnam(user_name)
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

    def _capture_conflicting_unit_state(self, unit_name: str) -> ConflictingUnitState:
        if not self._unit_exists(unit_name):
            return ConflictingUnitState(
                existed=False,
                enabled_before=False,
                active_before=False,
                masked_before=False,
                changed_by_installer=False,
            )

        enabled_state = self._read_unit_enabled_state(unit_name)
        active_state = self._read_unit_active_state(unit_name)
        return ConflictingUnitState(
            existed=True,
            enabled_before=enabled_state in {"enabled", "enabled-runtime"},
            active_before=active_state == "active",
            masked_before=enabled_state == "masked",
            changed_by_installer=False,
        )

    def _configure_conflicting_unit(self, unit_name: str, state: ConflictingUnitState) -> bool:
        if not state.existed:
            return False

        changed_by_installer = False
        if state.enabled_before or state.active_before:
            self._run_optional(["systemctl", "disable", "--now", unit_name])
            changed_by_installer = True
        if not state.masked_before:
            self._run_optional(["systemctl", "mask", unit_name])
            changed_by_installer = True
        return changed_by_installer

    def _read_unit_enabled_state(self, unit_name: str) -> str:
        completed = self._run(["systemctl", "is-enabled", unit_name], check=False)
        return (completed.stdout or "").strip()

    def _read_unit_active_state(self, unit_name: str) -> str:
        completed = self._run(["systemctl", "is-active", unit_name], check=False)
        return (completed.stdout or "").strip()

    def _load_uninstall_state(self) -> UninstallState:
        install_state_path = self._stage(self.paths.install_state_path)
        if not install_state_path.exists():
            return UninstallState(
                install_state_present=False,
                lib_dir=self.paths.lib_dir,
                share_dir=self.paths.share_dir,
                config_dir=self.paths.config_dir,
                config_path=self.paths.config_path,
                logind_override_path=self.paths.logind_override_path,
                service_home=self.paths.service_home,
                systemd_unit_paths=self.paths.systemd_unit_paths,
                service_user_created=False,
                getty_state=None,
                display_manager_state=None,
            )

        try:
            install_state = json.loads(install_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                f"Install-state file {self.paths.install_state_path} is unreadable: {exc.msg}"
            ) from exc

        managed_paths = install_state.get("managed_paths", {})
        conflicting_units = install_state.get("conflicting_units", {})
        service_user = install_state.get("service_user", {})

        return UninstallState(
            install_state_present=True,
            lib_dir=self._path_from_state(managed_paths, "lib_dir", self.paths.lib_dir),
            share_dir=self._path_from_state(managed_paths, "share_dir", self.paths.share_dir),
            config_dir=self._path_from_state(managed_paths, "config_dir", self.paths.config_dir),
            config_path=self._path_from_state(managed_paths, "config_path", self.paths.config_path),
            logind_override_path=self._path_from_state(
                managed_paths,
                "logind_override_path",
                self.paths.logind_override_path,
            ),
            service_home=self._path_from_state(managed_paths, "service_home", self.paths.service_home),
            systemd_unit_paths=self._systemd_unit_paths_from_state(managed_paths.get("systemd_units")),
            service_user_created=bool(service_user.get("created_by_installer", False)),
            getty_state=self._conflicting_unit_state_from_state(conflicting_units.get("getty@tty1.service")),
            display_manager_state=self._conflicting_unit_state_from_state(
                conflicting_units.get("display-manager.service")
            ),
        )

    def remove_service_units(self, unit_paths: Sequence[Path]) -> None:
        self.output("Removing relay systemd unit files")
        for unit_path in unit_paths:
            self._remove_file(unit_path)

    def remove_logind_override(self, logind_override_path: Path) -> None:
        self.output("Removing relay logind override")
        self._remove_file(logind_override_path)

    def restore_getty(self, state: ConflictingUnitState | None) -> None:
        self.output("Restoring getty@tty1.service")
        self._run_optional(["systemctl", "unmask", "getty@tty1.service"])
        if state is None or state.enabled_before:
            self._run_optional(["systemctl", "enable", "getty@tty1.service"])
        self._run_optional(["systemctl", "start", "getty@tty1.service"])

    def restore_display_manager(self, state: ConflictingUnitState | None) -> None:
        if state is None or not state.existed or not state.changed_by_installer:
            return

        self.output("Restoring display-manager.service from install-state")
        if not state.masked_before:
            self._run_optional(["systemctl", "unmask", "display-manager.service"])
        if state.enabled_before:
            self._run_optional(["systemctl", "enable", "display-manager.service"])
        if state.active_before:
            self._run_optional(["systemctl", "start", "display-manager.service"])

    def remove_runtime_tree(self, lib_dir: Path) -> None:
        self.output(f"Removing runtime package from {lib_dir}")
        self._remove_tree(lib_dir)

    def remove_shared_assets(self, share_dir: Path) -> None:
        self.output(f"Removing shared assets from {share_dir}")
        self._remove_tree(share_dir)

    def remove_runtime_state_dir(self) -> None:
        self.output(f"Removing runtime state under {RUNTIME_STATE_DIR}")
        self._remove_tree(RUNTIME_STATE_DIR)

    def remove_service_user(self, uninstall_state: UninstallState) -> None:
        if not uninstall_state.install_state_present:
            self.output(
                f"Leaving service user {SERVICE_USER!r} and {uninstall_state.service_home} intact because installer authorship is unknown"
            )
            return
        if not uninstall_state.service_user_created:
            self.output(
                f"Leaving service user {SERVICE_USER!r} and {uninstall_state.service_home} intact because the installer did not create them"
            )
            return
        if not self._service_user_exists():
            self.output(f"Service user {SERVICE_USER!r} is already absent")
            return

        self.output(f"Removing service user {SERVICE_USER!r}")
        self._run(["userdel", SERVICE_USER])

    def purge_config(self, config_path: Path, config_dir: Path) -> None:
        self.output(f"Purging relay config artifacts from {config_dir}")
        self._remove_file(config_path)

        staged_config_dir = self._stage(config_dir)
        if staged_config_dir.exists():
            for backup_path in sorted(staged_config_dir.glob(f"{config_path.name}.bak.*")):
                host_backup_path = config_dir / backup_path.name
                self._remove_file(host_backup_path)

        staged_config_dir = self._stage(config_dir)
        if staged_config_dir.exists() and staged_config_dir.is_dir() and not any(staged_config_dir.iterdir()):
            staged_config_dir.rmdir()
            self.output(f"Removed empty config directory {config_dir}")

    def remove_install_state(self) -> None:
        self.output(f"Removing install-state record at {self.paths.install_state_path}")
        self._remove_file(self.paths.install_state_path)

    def remove_service_home(self, uninstall_state: UninstallState) -> None:
        if not uninstall_state.install_state_present or not uninstall_state.service_user_created:
            return
        self.output(f"Removing service home {uninstall_state.service_home}")
        self._remove_tree(uninstall_state.service_home)

    def _path_from_state(self, mapping: object, key: str, default: Path) -> Path:
        if not isinstance(mapping, dict):
            return default
        value = mapping.get(key)
        if isinstance(value, str) and value.startswith("/"):
            return Path(value)
        return default

    def _systemd_unit_paths_from_state(self, value: object) -> tuple[Path, ...]:
        if not isinstance(value, list):
            return self.paths.systemd_unit_paths

        paths = tuple(Path(item) for item in value if isinstance(item, str) and item.startswith("/"))
        return paths or self.paths.systemd_unit_paths

    def _conflicting_unit_state_from_state(self, value: object) -> ConflictingUnitState | None:
        if not isinstance(value, dict):
            return None
        return ConflictingUnitState(
            existed=bool(value.get("existed", False)),
            enabled_before=bool(value.get("enabled_before", False)),
            active_before=bool(value.get("active_before", False)),
            masked_before=bool(value.get("masked_before", False)),
            changed_by_installer=bool(value.get("changed_by_installer", False)),
        )

    def _stage(self, host_path: Path) -> Path:
        relative = host_path.relative_to("/")
        return self.install_root / relative

    def _remove_file(self, host_path: Path) -> None:
        staged_path = self._stage(host_path)
        if not staged_path.exists() and not staged_path.is_symlink():
            return
        staged_path.unlink()
        self.output(f"Removed {host_path}")

    def _remove_tree(self, host_path: Path) -> None:
        staged_path = self._stage(host_path)
        if not staged_path.exists() and not staged_path.is_symlink():
            return
        if staged_path.is_dir() and not staged_path.is_symlink():
            rmtree(staged_path)
        else:
            staged_path.unlink()
        self.output(f"Removed {host_path}")

    def _write_text(self, path: Path, content: str, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    def _next_backup_path(self, config_path: Path) -> Path:
        timestamp = self.now_provider().astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
        return config_path.with_name(f"{config_path.name}.bak.{timestamp}")

    def _format_timestamp(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

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
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--purge-config", action="store_true")
    parser.add_argument("--skip-package-install", action="store_true")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--skip-host-validation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installer = HostBootstrapInstaller(repo_root=args.repo_root, install_root=args.root)

    try:
        if args.uninstall:
            if args.replace_config:
                raise BootstrapError("--replace-config is only valid for install")
            if args.skip_package_install:
                raise BootstrapError("--skip-package-install is only valid for install")
            if args.skip_host_validation:
                raise BootstrapError("--skip-host-validation is only valid for install")
            installer.uninstall(purge_config=args.purge_config)
        else:
            if args.purge_config:
                raise BootstrapError("--purge-config is only valid for uninstall")
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
