# RelayInnerDisplayScript

RelayInnerDisplayScript is a Proxmox-hosted display relay project for a single KVM guest.

The target outcome is a small appliance-like runtime that takes one VM managed by Proxmox and mirrors it directly onto a monitor attached to the Proxmox host. The host should boot into a kiosk session, show the guest through SPICE and `remote-viewer`, sleep or wake the monitor based on VM power state, and use the host power button as guest power control instead of shutting down the host.

## Status

This repository now includes Specs 10 through 17 of the MVP plan.

- The MVP architecture and behavior are defined in `./specs`.
- Spec 10 now has a Python implementation for config loading, daemon/session IPC, local Proxmox command wrappers, SPICE `.vv` generation, and reconnect state handling.
- Spec 11 now extends the runtime with a Cage session entrypoint, session-side waiting/degraded/sleeping view state, and Wayland display-power IPC handling.
- Spec 12 now adds display-policy config, daemon-side VM power to DPMS mapping, delayed display sleep, and power-intent reapplication after session reconnect.
- Spec 13 now extends the daemon with host power-button validation, evdev button capture, debounced guest start/shutdown forwarding, and runtime button-action tracking.
- Spec 14 now adds the host-direct bootstrap layer: a checked-in `install.sh`, sample config, setup guide, logind override rendering, and managed systemd unit installation for the daemon, kiosk, and seat runtime.
- Spec 15 now hardens the appliance integration layer with the final MVP state-file contract, runtime dependency validation, repeated Proxmox-failure degradation, subsystem-scoped journald logging, and systemd restart-loop thresholds.
- Spec 16 now turns the root README into the first-run install entrypoint and persists `/var/lib/relayinner-display/install-state.json` so later uninstall or upgrade work can see what the installer changed.
- Spec 17 now adds a root-owned `uninstall.sh`, install-state-aware host restoration, default config preservation, and an explicit purge path for full relay cleanup.
- The current design still assumes direct installation on a Proxmox host, not an LXC container.

## Quickstart

Use this install path only on a Proxmox VE host with `systemd`, a directly attached monitor, and one target guest that exposes a SPICE display.

1. Clone or copy this repository onto the Proxmox host and `cd` into the checkout.
2. Run `sudo ./install.sh`.
3. Edit `/etc/relayinner-display/config.toml` and set at least:
   - `[target].vmid`
   - `[target].node_name`
   - `[display].output_name` if you want to pin a specific connector name; this is recommended when using the default `wlr-randr` helper on hosts with more than one connector
   - `[input].power_button_event` if the default evdev path does not match the host
4. Restart the relay services after editing the config:

```sh
systemctl restart relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
```

5. Reboot once and verify that `tty1` returns directly into the kiosk session. For first-run checks, confirm the services are up and that `/var/lib/relayinner-display/install-state.json` exists:

```sh
systemctl status relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
sudo cat /var/lib/relayinner-display/install-state.json
```

For the full operator procedure, package assumptions, managed paths, troubleshooting commands, and installer flag details, see [`./docs/proxmox-host-setup.md`](./docs/proxmox-host-setup.md).

If the kiosk journal shows `libseat` errors such as `Could not connect to socket /run/seatd.sock: Permission denied`, `Could not open target tty: Permission denied`, or `Failed to start a DRM session`, refresh the installed units with `sudo ./install.sh` before debugging further. The current kiosk unit is expected to launch `cage -- /usr/local/lib/relayinner-display/session-entrypoint` while `relayinner-display-seatd.service` owns `/run/seatd.sock`; older installs that still wrap Cage with `seatd-launch` can exit immediately with `status=1`.

If the kiosk journal shows `failed to open /dev/dri/renderD128: Permission denied`, `failed to open /dev/dri/card0: Permission denied`, or `Unable to create the wlroots renderer`, rerun `sudo ./install.sh` so the generated kiosk unit picks up the host DRM groups with `SupplementaryGroups=...`, typically `video render`.

The kiosk unit also forces `LIBSEAT_BACKEND=seatd` so `cage` uses the same seatd backend that succeeded in transient `systemd-run` debugging instead of relying on libseat backend auto-selection.

If `systemctl status relayinner-display-kiosk.service` shows `cage` starting and then failing a few seconds later while the child process is still `/usr/bin/python3 /usr/local/lib/relayinner-display/session-entrypoint`, refresh the installed runtime with `sudo ./install.sh`. Older installs can still carry the pre-hotfix `relayinner_display/kiosk.py` that tried to exec `relayinner-display-session` by bare name; when `cage` drops `PATH`, that launcher exits immediately and systemd records `status=1`.

If the kiosk journal or a direct `runuser -u relayinner-display -- /usr/local/lib/relayinner-display/session-entrypoint` test shows `PermissionError: [Errno 13] Permission denied: '/etc/relayinner-display/config.toml'`, rerun `sudo ./install.sh` so the installer refreshes `/etc/relayinner-display/` and `config.toml` with relay-group-readable permissions. The session process runs as `relayinner-display`, so the config must be readable by that service group even when the content is preserved across installs.

If display sleep or wake fails and the kiosk journal shows `Wayland server does not support wlr-output-power-management-v1`, the host is still using an older `power_helper = "wlopm"` setting. Cage reliably supports output changes through `wlr-randr`, not the `wlopm` protocol on all versions, so refresh the install with `sudo ./install.sh` and keep the default `power_helper = "wlr-randr"` unless you have verified compositor-side `wlopm` support.

If `remote-viewer` starts and then exits with a generic dialog such as `Unable to connect graphic server /run/relayinner-display/current.vv`, verify the generated `.vv` file keeps certificate and other multiline values on one escaped line. Proxmox returns the SPICE `ca` field with embedded `\n` escapes; writing that back as literal newlines corrupts the INI-style `.vv` file and can make `remote-viewer` fail immediately even though `pvesh ... spiceproxy` succeeded.

## Uninstall

To remove the relay appliance and return the host to its normal local-login path, run:

```sh
sudo ./uninstall.sh
```

Default uninstall preserves `/etc/relayinner-display/config.toml`, stops and disables the relay services, removes relay-managed runtime assets and host overrides, and restores `getty@tty1.service`.

For full cleanup, including `/etc/relayinner-display/config.toml` and `config.toml.bak.*` backups, run:

```sh
sudo ./uninstall.sh --purge-config
```

For the detailed removal contract, best-effort fallback behavior, and post-uninstall recovery checks, see [`./docs/proxmox-host-setup.md`](./docs/proxmox-host-setup.md).

## MVP Goals

- Relay one Proxmox VM to one host-attached display.
- Use `Cage` as the kiosk shell and `remote-viewer` as the SPICE client.
- Control the target VM locally with `qm` and `pvesh`.
- Put the monitor into standby when the VM is off and wake it when the VM is active.
- Map the host power button to guest start or graceful shutdown behavior.

## Non-goals for MVP

- Multiple VM switching
- Audio, clipboard, USB policy, and file sharing
- noVNC or browser viewing
- Cluster migration tracking
- Host suspend or general-purpose desktop access
- Packaging inside Proxmox LXC

## Planned Runtime Shape

The current MVP design assumes:

- Proxmox host direct install
- Python scripts plus systemd services
- `relayinner-displayd` as the root-owned control daemon
- `relayinner-display-session` as the Cage session supervisor
- local IPC over a Unix socket in `/run/relayinner-display/`
- persistent config in `/etc/relayinner-display/config.toml`

Current implementation coverage:

- `relayinner_display.config` validates the shared TOML config model from Specs 10, 12, and 13.
- `relayinner_display.proxmox` wraps local `qm` and `pvesh` calls, writes `remote-viewer` `.vv` files, and submits guest start/shutdown requests.
- `relayinner_display.daemon` now owns the end-to-end appliance state machine, validates required runtime binaries, degrades after repeated local Proxmox failures, captures host power-button intent, and writes the Spec 15 runtime state contract to disk.
- `relayinner_display.input` validates host `logind` power-key policy and captures `KEY_POWER` presses from one evdev node.
- `relayinner_display.session` supervises `remote-viewer`, tracks waiting/degraded/display-sleeping session state, applies Wayland display-power actions through `wlr-randr` by default while preserving custom helper support, and emits subsystem-scoped session, console, and display logs.
- `relayinner_display.kiosk` provides the Cage session entrypoint and the canonical `cage -- ...` command shape against the managed `relayinner-display-seatd.service`.
- `relayinner_display.bootstrap` renders the sample config, systemd units, logind override, host-detected DRM supplementary groups for the kiosk unit, the Spec 15 `StartLimitIntervalSec=120` / `StartLimitBurst=5` restart-loop policy, the Spec 16 install-state record under `/var/lib/relayinner-display/install-state.json`, and the Spec 17 uninstall flow that restores `tty1` plus optional display-manager state conservatively.
- `tests/` now cover config parsing, IPC validation, Proxmox command handling, reconnect logic, daemon DPMS debounce behavior, Spec 15 state persistence, runtime dependency degradation, restart-threshold rendering, install-state persistence, uninstall fallback and purge behavior, session supervision, logind policy parsing, power-button handling, display-power handling, and kiosk entrypoint wiring.

Operationally, the appliance is expected to move through a small state machine:

- `booting`
- `waiting_for_session`
- `waiting_for_vm`
- `requesting_console`
- `showing_console`
- `reconnecting_console`
- `display_sleeping`
- `degraded`

## Spec Set

- [Spec index](./specs/README.md)
- [Spec 10: Proxmox Local Console Relay Core](./specs/10-proxmox-local-console-relay-core.md)
- [Spec 11: Cage Kiosk Session Shell](./specs/11-cage-kiosk-session-shell.md)
- [Spec 12: VM Power-State to Host DPMS Control](./specs/12-vm-power-state-to-host-dpms-control.md)
- [Spec 13: Host Power Button to Guest Power Control](./specs/13-host-power-button-to-guest-power-control.md)
- [Spec 14: Proxmox Host Runtime and Bootstrap](./specs/14-proxmox-host-runtime-and-bootstrap.md)
- [Spec 15: MVP Integration, Failure Policy, and Ops](./specs/15-mvp-integration-failure-policy-and-ops.md)
- [Spec 16: Proxmox Host Installation Flow and README Quickstart](./specs/16-proxmox-host-installation-flow-and-readme-quickstart.md)
- [Spec 17: Safe Uninstall Flow and README Removal Guide](./specs/17-safe-uninstall-flow-and-readme-removal-guide.md)

Recommended implementation order:

1. Spec 10
2. Spec 11
3. Spec 12 and Spec 13
4. Spec 14
5. Spec 15
6. Spec 16
7. Spec 17

## Expected Host Dependencies

The MVP spec currently assumes these host-side packages or equivalents:

- `python3`
- `python3-evdev`
- `cage`
- `seatd`
- `virt-viewer`
- `wlr-randr`

The current implementation now manages:

- systemd service units for daemon, kiosk session, and seat handling
- a non-login runtime user through `install.sh`
- a `logind` override for host power-button behavior
- an install-state record under `/var/lib/relayinner-display/install-state.json`
- runtime state under `/run/relayinner-display/`
- subsystem-scoped journald observability for `proxmox`, `session`, `console`, `display`, and `input`
- restart-loop thresholds of 5 failures within 2 minutes for the managed systemd units
- a checked-in sample config and host setup guide

## Repository Layout

```text
.
├── README.md
├── AGENTS.md
├── config.example.toml
├── docs/
├── install.sh
├── uninstall.sh
├── relayinner_display/
├── specs/
├── tests/
└── tasks/
```

- `relayinner_display/` holds the current Python runtime for Specs 10 through 16.
- `config.example.toml` is the host bootstrap sample config installed by Specs 14 and 16.
- `docs/` holds operator-facing setup documentation for the host-direct install path.
- `install.sh` is the idempotent host bootstrap entrypoint from Specs 14 and 16.
- `uninstall.sh` is the safe removal entrypoint from Spec 17.
- `specs/` holds the MVP specification set.
- `tests/` holds unit tests for the current runtime slice.
- `tasks/` is reserved for task/worktree-oriented workflow.

## Intended Operator Experience

After the project is installed, the intended flow is:

1. Install the runtime on a Proxmox host that has a directly attached monitor.
2. Configure one target `vmid`.
3. Reboot or start the services.
4. The host enters a Cage kiosk session automatically.
5. If the VM is running, the guest appears fullscreen.
6. If the VM is off, the display waits briefly and then sleeps.
7. Pressing the host power button starts the VM when it is off, or requests graceful shutdown when it is on.
8. If a local dependency or repeated control-path failure occurs, the monitor stays on a controlled degraded view and the reason is visible in journald plus `/run/relayinner-display/daemon.state.json`.

## Notes

- The design is intentionally narrow to reach a usable MVP quickly.
- The current specs assume the target guest is a desktop-style VM, with Windows as the default operator profile.
- Because the control path is local to the Proxmox host, the MVP does not require storing a remote Proxmox API token.
