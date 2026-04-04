# RelayInnerDisplayScript

RelayInnerDisplayScript is a Proxmox-hosted display relay project for a single KVM guest.

The target outcome is a small appliance-like runtime that takes one VM managed by Proxmox and mirrors it directly onto a monitor attached to the Proxmox host. The host should boot into a kiosk session, show the guest through SPICE and `remote-viewer`, sleep or wake the monitor based on VM power state, and use the host power button as guest power control instead of shutting down the host.

## Status

This repository now includes the first five runtime slices for the MVP.

- The MVP architecture and behavior are defined in `./specs`.
- Spec 10 now has a Python implementation for config loading, daemon/session IPC, local Proxmox command wrappers, SPICE `.vv` generation, and reconnect state handling.
- Spec 11 now extends the runtime with a Cage session entrypoint, session-side waiting/degraded/sleeping view state, and Wayland display-power IPC handling.
- Spec 12 now adds display-policy config, daemon-side VM power to DPMS mapping, delayed display sleep, and power-intent reapplication after session reconnect.
- Spec 13 now extends the daemon with host power-button validation, evdev button capture, debounced guest start/shutdown forwarding, and runtime button-action tracking.
- Spec 14 now adds the host-direct bootstrap layer: a checked-in `install.sh`, sample config, setup guide, logind override rendering, and managed systemd unit installation for the daemon, kiosk, and seat runtime.
- The current design still assumes direct installation on a Proxmox host, not an LXC container.
- Final failure-policy and ops hardening remain pending in Spec 15.

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
- `relayinner_display.daemon` owns the VM/session state machine, evaluates VM power to display-power policy, debounces display sleep, captures host power-button intent, and writes the expanded runtime state to disk.
- `relayinner_display.input` validates host `logind` power-key policy and captures `KEY_POWER` presses from one evdev node.
- `relayinner_display.session` supervises `remote-viewer`, tracks waiting/degraded/display-sleeping session state, applies `wlopm`-style display-power actions from the Wayland session context, and reapplies daemon power intent after reconnect.
- `relayinner_display.kiosk` provides the Cage session entrypoint and the canonical `seatd-launch -- cage -- ...` command shape from Spec 11.
- `relayinner_display.bootstrap` renders the sample config, systemd units, and logind override, and applies the direct-host installer flow from Spec 14.
- `tests/` covers config parsing, IPC validation, Proxmox command handling, reconnect logic, daemon DPMS debounce behavior, session supervision, logind policy parsing, power-button handling, display-power handling, and kiosk entrypoint wiring.

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

Recommended implementation order:

1. Spec 10
2. Spec 11
3. Spec 12 and Spec 13
4. Spec 14
5. Spec 15

## Expected Host Dependencies

The MVP spec currently assumes these host-side packages or equivalents:

- `python3`
- `python3-evdev`
- `cage`
- `seatd`
- `virt-viewer`
- `wlopm`

The current implementation now manages:

- systemd service units for daemon, kiosk session, and seat handling
- a non-login runtime user through `install.sh`
- a `logind` override for host power-button behavior
- runtime state under `/run/relayinner-display/`
- a checked-in sample config and host setup guide

## Repository Layout

```text
.
├── README.md
├── AGENTS.md
├── config.example.toml
├── docs/
├── install.sh
├── relayinner_display/
├── specs/
├── tests/
└── tasks/
```

- `relayinner_display/` holds the current Python runtime for Specs 10 through 14.
- `config.example.toml` is the host bootstrap sample config installed by Spec 14.
- `docs/` holds operator-facing setup documentation for the host-direct install path.
- `install.sh` is the idempotent host bootstrap entrypoint from Spec 14.
- `specs/` holds the MVP specification set.
- `tests/` holds unit tests for the current runtime slice.
- `tasks/` is reserved for task/worktree-oriented workflow.

## Intended Operator Experience

After the project is implemented, the intended flow is:

1. Install the runtime on a Proxmox host that has a directly attached monitor.
2. Configure one target `vmid`.
3. Reboot or start the services.
4. The host enters a Cage kiosk session automatically.
5. If the VM is running, the guest appears fullscreen.
6. If the VM is off, the display waits briefly and then sleeps.
7. Pressing the host power button starts the VM when it is off, or requests graceful shutdown when it is on.

## Notes

- The design is intentionally narrow to reach a usable MVP quickly.
- The current specs assume the target guest is a desktop-style VM, with Windows as the default operator profile.
- Because the control path is local to the Proxmox host, the MVP does not require storing a remote Proxmox API token.
