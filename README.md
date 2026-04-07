# RelayInnerDisplayScript

RelayInnerDisplayScript is a Proxmox-hosted display relay project for a single KVM guest.

The target outcome is a small appliance-like runtime that takes one VM managed by Proxmox and mirrors it directly onto a monitor attached to the Proxmox host. The host should boot into a kiosk session, show the guest through SPICE or loopback-only VNC with `remote-viewer`, through Looking Glass with `looking-glass-client`, or through Moonlight with Linux `moonlight-qt`, sleep or wake the monitor based on VM power state, and use the host power button as guest power control instead of shutting down the host.

## Status

This repository now includes Specs 10 through 17 of the current MVP plan, Specs 20 through 22 for the implemented console-backend expansion series, and Specs 30 through 32 for the completed Moonlight client slice.

- The MVP architecture and behavior are defined in `./specs`.
- Spec 10 now has a Python implementation for config loading, daemon/session IPC, local Proxmox command wrappers, SPICE `.vv` generation, and reconnect state handling.
- Spec 11 now extends the runtime with a Cage session entrypoint, session-side waiting/degraded/sleeping view state, and Wayland display-power IPC handling.
- Spec 12 now adds display-policy config, daemon-side VM power to DPMS mapping, delayed display sleep, and power-intent reapplication after session reconnect.
- Spec 13 now extends the daemon with host power-button validation, evdev button capture, debounced guest start/shutdown forwarding, and runtime button-action tracking.
- Spec 14 now adds the host-direct bootstrap layer: a checked-in `install.sh`, sample config, setup guide, logind override rendering, and managed systemd unit installation for the daemon, kiosk, and seat runtime.
- Spec 15 now hardens the appliance integration layer with the final MVP state-file contract, runtime dependency validation, repeated Proxmox-failure degradation, subsystem-scoped journald logging, and systemd restart-loop thresholds.
- Spec 16 now turns the root README into the first-run install entrypoint and persists `/var/lib/relayinner-display/install-state.json` so later uninstall or upgrade work can see what the installer changed.
- Spec 17 now adds a root-owned `uninstall.sh`, install-state-aware host restoration, default config preservation, and an explicit purge path for full relay cleanup.
- Spec 20 now generalizes the shared config, runtime artifact layout, IPC, session launch path, and runtime state around a backend-neutral console contract while keeping existing SPICE behavior intact.
- Spec 21 now implements the loopback-only Proxmox VNC backend, including config validation, `qm config` matching, endpoint probing, runtime `vnc_endpoint` state, and `remote-viewer` URI launch.
- Spec 22 now implements the preflight-only Looking Glass backend, including config validation, shared-memory preflight, runtime `looking_glass_shm_file` state, and fullscreen `looking-glass-client` launch wiring.
- Spec 30 now implements the initial Moonlight backend contract for Sunshine-backed guests, including config validation, managed workspace preparation, `moonlight-qt` version gating, and fullscreen launch wiring through the existing kiosk/session model.
- Spec 31 now extends the Moonlight path with relay-managed persistent pairing state, daemon-side `list` checks, session-launched `moonlight pair --pin` assist, `waiting_for_pairing` session flow, and runtime pairing metadata without storing Sunshine web-UI credentials.
- Spec 32 now completes the Moonlight path with live app-list validation, `--quit-after` launch rules, reconnect recovery, `moonlight_app` runtime metadata, and operator-facing ops documentation.
- The current design still assumes direct installation on a Proxmox host, not an LXC container.

## Quickstart

Use this install path only on a Proxmox VE host with `systemd`, a directly attached monitor, and one target guest that exposes either a SPICE display, an operator-prepared loopback-only VNC endpoint, a fully operator-prepared Looking Glass guest, or a Sunshine host reachable from the Proxmox host for Moonlight.

1. Clone or copy this repository onto the Proxmox host and `cd` into the checkout.
2. Run `sudo ./install.sh`.
3. Edit `/etc/relayinner-display/config.toml` and set at least:
   - `[target].vmid`
   - `[target].node_name`
   - `[target].console_backend` if you are switching away from the default SPICE path
   - `[console.vnc].display_number` plus matching VM `args: -vnc 127.0.0.1:<display_number>` when using `console_backend = "vnc"`
   - `[console.looking_glass].shm_file` plus any renderer or SPICE overrides when using `console_backend = "looking-glass"`
   - `[console.moonlight].host` plus any non-default `app`, `base_port`, or `state_dir` when using `console_backend = "moonlight"`
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

When `console_backend = "moonlight"` and the Sunshine host is reachable but not yet paired, the kiosk now enters `waiting_for_pairing`, launches Moonlight's pairing UI fullscreen with a 4-digit PIN, and resumes automatically after you approve that PIN in the Sunshine web UI `PIN` page on the guest. The daemon keeps polling pairing completion while that pairing UI is still open, so a successful approval advances into the configured stream without requiring a manual kiosk click or a service restart. The active PIN is also mirrored in `/run/relayinner-display/daemon.state.json` while approval is pending. Once paired, the daemon validates `[console.moonlight].app` against the live `moonlight list --csv` output with case-insensitive matching before it launches the configured app fullscreen. This relay slice does not store Sunshine usernames or passwords.

If the kiosk journal shows `libseat` errors such as `Could not connect to socket /run/seatd.sock: Permission denied`, `Could not open target tty: Permission denied`, or `Failed to start a DRM session`, refresh the installed units with `sudo ./install.sh` before debugging further. The current kiosk unit is expected to launch `cage -- /usr/local/lib/relayinner-display/session-entrypoint` while `relayinner-display-seatd.service` owns `/run/seatd.sock`; older installs that still wrap Cage with `seatd-launch` can exit immediately with `status=1`.

If the kiosk journal shows `failed to open /dev/dri/renderD128: Permission denied`, `failed to open /dev/dri/card0: Permission denied`, or `Unable to create the wlroots renderer`, rerun `sudo ./install.sh` so the generated kiosk unit picks up the host DRM groups with `SupplementaryGroups=...`, typically `video render`.

The kiosk unit also forces `LIBSEAT_BACKEND=seatd` so `cage` uses the same seatd backend that succeeded in transient `systemd-run` debugging instead of relying on libseat backend auto-selection.

If `systemctl status relayinner-display-kiosk.service` shows `cage` starting and then failing a few seconds later while the child process is still `/usr/bin/python3 /usr/local/lib/relayinner-display/session-entrypoint`, refresh the installed runtime with `sudo ./install.sh`. Older installs can still carry the pre-hotfix `relayinner_display/kiosk.py` that tried to exec `relayinner-display-session` by bare name; when `cage` drops `PATH`, that launcher exits immediately and systemd records `status=1`.

If the kiosk journal or a direct `runuser -u relayinner-display -- /usr/local/lib/relayinner-display/session-entrypoint` test shows `PermissionError: [Errno 13] Permission denied: '/etc/relayinner-display/config.toml'`, rerun `sudo ./install.sh` so the installer refreshes `/etc/relayinner-display/` and `config.toml` with relay-group-readable permissions. The session process runs as `relayinner-display`, so the config must be readable by that service group even when the content is preserved across installs.

If display sleep or wake fails and the kiosk journal shows `Wayland server does not support wlr-output-power-management-v1`, the host is still using an older `power_helper = "wlopm"` setting. Cage reliably supports output changes through `wlr-randr`, not the `wlopm` protocol on all versions, so refresh the install with `sudo ./install.sh` and keep the default `power_helper = "wlr-randr"` unless you have verified compositor-side `wlopm` support.

If `remote-viewer` starts and then exits with a generic dialog such as `Unable to connect graphic server /run/relayinner-display/console/spice-current.vv`, verify the generated `.vv` file keeps certificate and other multiline values on one escaped line. Proxmox returns the SPICE `ca` field with embedded `\n` escapes; writing that back as literal newlines corrupts the INI-style `.vv` file and can make `remote-viewer` fail immediately even though `pvesh ... spiceproxy` succeeded.

If the appliance enters `degraded` with `backend=vnc`, verify that `qm config <vmid>` contains `args: -vnc 127.0.0.1:<display_number>` and that `[console.vnc].display_number` matches exactly. Non-loopback VNC binds are refused intentionally. If the VM is running but the kiosk stays in reconnecting flow, inspect `vnc_endpoint` in `/run/relayinner-display/daemon.state.json`; the relay waits there until the loopback VNC socket accepts TCP connections.

If the appliance enters `degraded` with `backend=looking-glass`, verify that `looking-glass-client` is installed, `[console.looking_glass].shm_file` already exists, and the file or device is readable by the `relayinner-display` session user. This backend is preflight-only support; GPU passthrough, KVMFR/IVSHMEM device creation, and guest host-app installation remain operator-managed. The upstream setup guidance lives at <https://looking-glass.io/docs/stable/requirements/> and <https://looking-glass.io/docs/stable/install_client/>.

If the appliance enters `degraded` with `backend=moonlight`, verify that Linux `moonlight-qt` version `6.0.0` or newer is installed, `[console.moonlight].host`, `base_port`, and `app` point at the expected Sunshine host and app, and the managed `state_dir` is writable by the relay session user. The relay matches `app` case-insensitively against the live Sunshine app list, and `quit_app_after_session = true` is only valid for non-`Desktop` apps. If the kiosk instead stays in `waiting_for_pairing`, approve the PIN shown by Moonlight's pairing UI or in `/run/relayinner-display/daemon.state.json` on the Sunshine web UI `PIN` page.

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
- Use `Cage` as the kiosk shell and a curated console client: `remote-viewer` for SPICE or VNC, `looking-glass-client` for Looking Glass, or Linux `moonlight-qt` for Sunshine-backed guests.
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

- `relayinner_display.config` now validates the shared TOML config model from Specs 10, 12, 13, 20, 21, 22, 30, and 32, including the backend-neutral `[console]` namespace, loopback-only VNC settings, Looking Glass preflight options, Moonlight host/workspace config, the `Desktop` plus `quit_app_after_session` exclusion rule, and legacy SPICE path compatibility.
- `relayinner_display.proxmox` wraps local `qm` and `pvesh` calls, writes `remote-viewer` `.vv` files, validates loopback-only VNC `qm config` exposure, probes the derived VNC socket, and submits guest start/shutdown requests.
- `relayinner_display.daemon` now owns the end-to-end appliance state machine, validates required runtime binaries, emits backend-neutral `connect_console` IPC, prepares SPICE, VNC, Looking Glass, or Moonlight console launches, runs Looking Glass shared-memory preflight, prepares the managed Moonlight workspace with `portable.dat`, enforces `moonlight-qt >= 6.0.0`, probes Moonlight host reachability, runs daemon-side `moonlight list` from the persistent workspace as the session user, launches Moonlight's own pairing UI inside the kiosk session with a relay-generated PIN when the host is unpaired, validates the configured Moonlight app from live CSV app-list output before each launch, re-enters reconnect flow after unexpected Moonlight exits, exposes `waiting_for_pairing` plus `moonlight_app` runtime metadata, degrades after repeated local Proxmox failures, captures host power-button intent, and writes the expanded runtime state contract to disk.
- `relayinner_display.input` validates host `logind` power-key policy and captures `KEY_POWER` presses from one evdev node.
- `relayinner_display.session` now validates backend/launcher allowlists for generic console launches, keeps legacy `connect_spice` compatibility during the transition window, tracks waiting/degraded/display-sleeping plus `waiting_for_pairing` session state, accepts daemon-provided waiting `details` for pair-assist instructions, launches `looking-glass-client`, Moonlight's pairing UI, or Moonlight streaming on the same curated contract as `remote-viewer`, honors daemon-provided working directories for managed backends, emits backend-tagged `console_exited` events for reconnect handling, applies Wayland display-power actions through `wlr-randr` by default while preserving custom helper support, and emits subsystem-scoped session, console, and display logs.
- `relayinner_display.kiosk` provides the Cage session entrypoint and the canonical `cage -- ...` command shape against the managed `relayinner-display-seatd.service`.
- `relayinner_display.bootstrap` renders the sample config, systemd units, logind override, host-detected DRM supplementary groups for the kiosk unit, the Spec 15 `StartLimitIntervalSec=120` / `StartLimitBurst=5` restart-loop policy, the Spec 16 install-state record under `/var/lib/relayinner-display/install-state.json`, the Spec 22 Looking Glass sample config hints, the Spec 30 and 32 Moonlight sample config hints, and the Spec 17 uninstall flow that restores `tty1` plus optional display-manager state conservatively.
- `tests/` now cover config parsing, backend-neutral IPC validation including `cwd` and waiting `details`, Proxmox command handling, SPICE, VNC, Looking Glass, and Moonlight launch/reconnect logic, Moonlight pair-assist polling and PIN renewal, live Moonlight app-list validation, daemon DPMS debounce behavior, Moonlight workspace/version validation, runtime-state/backend handling including `vnc_endpoint`, `looking_glass_shm_file`, `moonlight_app`, and Moonlight pairing metadata, runtime dependency degradation, restart-threshold rendering, install-state persistence, uninstall fallback and purge behavior, session supervision, logind policy parsing, power-button handling, display-power handling, and kiosk entrypoint wiring.

Operationally, the appliance is expected to move through a small state machine:

- `booting`
- `waiting_for_session`
- `waiting_for_vm`
- `waiting_for_pairing`
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
- [Spec 20: Configurable Console Backend Contract](./specs/20-configurable-console-backend-contract.md)
- [Spec 21: Proxmox Local VNC Backend](./specs/21-proxmox-local-vnc-backend.md)
- [Spec 22: Looking Glass Backend and Preflight](./specs/22-looking-glass-backend-and-preflight.md)
- [Spec 30: Moonlight Backend Contract and Config](./specs/30-moonlight-backend-contract-and-config.md)
- [Spec 31: Moonlight Pair-Assist and Persistent Workspace](./specs/31-moonlight-pair-assist-and-persistent-workspace.md)
- [Spec 32: Moonlight Stream Launch, Recovery, and Ops](./specs/32-moonlight-stream-launch-recovery-and-ops.md)

Recommended implementation order:

1. Spec 10
2. Spec 11
3. Spec 12 and Spec 13
4. Spec 14
5. Spec 15
6. Spec 16
7. Spec 17
8. Spec 20
9. Spec 21 and Spec 22
10. Spec 30
11. Spec 31
12. Spec 32

Console-backend expansion status:

- Wave 1 is complete: Spec 20 generalized the shared console contract without regressing SPICE.
- Wave 2 is complete: Spec 21 now owns the shipped VNC path, and Spec 22 now ships the preflight-only Looking Glass path on the same shared contract.
- Wave 3 is complete: Specs 30 through 32 now ship the Moonlight backend contract, persistent pairing workspace, live app-list validation, fullscreen stream launch, and reconnect behavior for Sunshine-backed guests.

## Expected Host Dependencies

The MVP spec currently assumes these host-side packages or equivalents:

- `python3`
- `python3-evdev`
- `cage`
- `seatd`
- `virt-viewer`
- `wlr-randr`

When `console_backend = "looking-glass"`, operators also need a working `looking-glass-client` install plus the upstream guest/passthrough prerequisites described at <https://looking-glass.io/docs/stable/requirements/> and <https://looking-glass.io/docs/stable/install_client/>. The relay does not automate those steps.

When `console_backend = "moonlight"`, operators also need Linux `moonlight-qt` version `6.0.0` or newer plus a guest that already runs Sunshine. The relay prepares the managed Moonlight workspace, keeps paired-host state there across restarts, validates the configured app name from live `moonlight list --csv` output before each launch, launches Moonlight's own pairing UI with a relay-generated PIN when the host is not yet paired, and drives approval through the Sunshine web UI `PIN` page, but it still does not automate Sunshine setup or store Sunshine usernames or passwords.

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

- `relayinner_display/` holds the current Python runtime for Specs 10 through 17 plus the Spec 20 shared console contract layer, the Spec 21/22 VNC and Looking Glass backends, and the Spec 30/31/32 Moonlight backend, pair-assist, and recovery layers.
- `config.example.toml` is the host bootstrap sample config installed by Specs 14, 16, 20, 21, 22, 30, 31, and 32.
- `docs/` holds operator-facing setup documentation for the host-direct install path.
- `install.sh` is the idempotent host bootstrap entrypoint from Specs 14 and 16.
- `uninstall.sh` is the safe removal entrypoint from Spec 17.
- `specs/` holds the MVP specification set plus the next console-backend expansion specs.
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
   A start request wakes the display back to the waiting view immediately, even before Proxmox reports the VM as fully `running`.
8. If a local dependency or repeated control-path failure occurs, the monitor stays on a controlled degraded view and the reason is visible in journald plus `/run/relayinner-display/daemon.state.json`.

## Notes

- The design is intentionally narrow to reach a usable MVP quickly.
- The current specs assume the target guest is a desktop-style VM, with Windows as the default operator profile.
- Because the control path is local to the Proxmox host, the MVP does not require storing a remote Proxmox API token.
