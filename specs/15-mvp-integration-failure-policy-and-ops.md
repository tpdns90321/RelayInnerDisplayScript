# Spec 15. MVP Integration, Failure Policy, and Ops

## Context / Problem

By the time Specs 10 through 14 are implemented, the MVP will include several interacting parts:

- Proxmox local command polling and SPICE material generation
- Cage and the local session supervisor
- `remote-viewer` lifecycle management
- monitor power control
- physical power-button control
- host bootstrap and policy overrides

Each of those can work in isolation while the overall appliance still feels unreliable or ambiguous. MVP needs one integration spec that fixes end-to-end state behavior, failure boundaries, restart expectations, and operator-visible observability.

## Goals / Non-goals

Goals:

- Define one end-to-end appliance state machine.
- Specify how the system behaves under the most likely operational faults.
- Define the minimum logging and troubleshooting contract for MVP pilots.
- Freeze the MVP feature boundary so implementation does not sprawl.

Non-goals:

- Central metrics backend integration
- Fleet management
- Cross-node VM migration handling
- GUI diagnostics panels
- Out-of-band remote administration tooling

## User stories

- As an operator, I can inspect logs and tell why the appliance is not showing the target VM.
- As an operator, I can reboot the Proxmox host and expect the appliance to converge automatically.
- As an operator, I can distinguish a VM-off state from a local display-stack failure.
- As a maintainer, I can reject out-of-scope MVP additions because the operational boundary is explicit.

## Public API / Interfaces

Appliance states:

- `booting`
- `waiting_for_session`
- `waiting_for_vm`
- `requesting_console`
- `showing_console`
- `reconnecting_console`
- `display_sleeping`
- `degraded`

State transition rules:

- `booting -> waiting_for_session` when the daemon is up but the Cage session has not reported ready
- `waiting_for_session -> waiting_for_vm` when the session reports ready and the VM is off
- `waiting_for_session -> requesting_console` when the session reports ready and the VM is active
- `requesting_console -> showing_console` when the session reports `console_started`
- `showing_console -> reconnecting_console` when the viewer exits unexpectedly while the VM remains active
- `waiting_for_vm -> display_sleeping` when the VM remains off longer than `dpms_off_delay_ms`
- any state -> `degraded` on repeated local unrecoverable faults such as invalid config, missing required binaries, missing power-button override, or local Proxmox command failure beyond retry policy

Failure policy:

- Viewer crash while VM remains active: reconnect with exponential backoff
- Session crash: restart through systemd and restore the last known daemon-driven state
- Daemon crash: restart through systemd and rebuild runtime state from config plus current VM status
- Missing required host dependency: enter `degraded` and keep controlled waiting screen if the session exists
- Transitional VM states: keep current screen state until the next stable evaluation

Systemd restart policy:

- `relayinner-display-kiosk.service`: `Restart=always`
- `relayinner-displayd.service`: `Restart=always`
- enter `degraded` after 5 restart failures within 2 minutes for the same subsystem

Logging contract:

- every appliance state transition
- every accepted power-button event and chosen action
- every display-power transition
- every Proxmox command failure
- every viewer launch and exit

Operator-visible MVP boundaries:

- supported: one host, one VM, one display, SPICE relay, power-button start and shutdown
- unsupported: audio, clipboard, USB policy, multi-VM switching, VM migration tracking, host suspend, force-stop actions

## Data model / Persistence

Operational state file:

- `/run/relayinner-display/daemon.state.json`

Required fields:

```json
{
  "appliance_state": "showing_console",
  "session_ready": true,
  "vm_power_state": "running",
  "display_power_applied": "on",
  "degraded_reason": null,
  "last_console_exit": null
}
```

Log retention:

- journald only in MVP
- no external metrics store
- no persistent event history beyond system logs

Persistence rules:

- The degraded reason is runtime-only.
- The daemon reconstructs current state from config, session readiness, and fresh `qm` status after restart.

## Security model (Permission/Isolation/Audit)

Permissions:

- Reuse the root daemon and unprivileged session split from earlier specs.
- Do not add privileged helper binaries beyond the direct-host requirements already established.

Isolation:

- `degraded` must never fall back to an interactive shell or general Proxmox login prompt.
- Failures must leave the monitor in a controlled waiting, reconnecting, sleeping, or degraded view.

Audit:

- Logs must identify the subsystem: `proxmox`, `session`, `console`, `display`, `input`, `install`.
- Each degraded condition must emit one concise root-cause message and avoid flooding the journal.

## Acceptance criteria (Testable, Verifiable)

- From a cold boot with the VM off, the appliance reaches `display_sleeping` without manual intervention.
- From a cold boot with the VM on, the appliance reaches `showing_console` without manual intervention.
- If the host power-button override is missing, the appliance reaches `degraded` with a clear reason and does not attempt to own the button.
- If the viewer process crashes while the VM remains active, the appliance returns to `showing_console` after reconnect attempts.
- If a required binary such as `remote-viewer` is missing, the appliance reaches `degraded` and exposes the reason in logs.

## Test plan

End-to-end scenarios:

- cold boot, VM off
- cold boot, VM on
- boot with invalid config
- boot with missing `remote-viewer`
- boot with missing power-button device when forwarding is enabled
- kill the viewer during active display
- start the VM from the host power button
- shut down the VM from the host power button

Operational checks:

- verify journald entries for each subsystem
- verify restart-loop thresholds and transition into `degraded`
- verify the waiting, sleeping, and degraded screens match internal state

Pilot exit criteria:

- all core scenarios above pass on at least one target Proxmox host and one Windows guest
- no crash path exposes a shell or unmanaged desktop

## Rollout / Backward compatibility

This spec freezes the MVP operational contract. Any additional feature proposed during implementation must either:

- fit inside the stated MVP boundary without changing operator expectations, or
- be deferred to a later spec series

Backward compatibility for the MVP line means preserving:

- config path `/etc/relayinner-display/config.toml`
- runtime path `/run/relayinner-display`
- service names beginning with `relayinner-display`

## Open questions

- Whether MVP needs a small local status CLI in addition to journald for first-line troubleshooting.
- Whether repeated local Proxmox command failures should eventually trigger a slower retry cadence than ordinary viewer reconnect failures.
- Whether the degraded screen should display a short operator code such as `E_CONFIG` or remain text-only.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
- Spec 11. Cage Kiosk Session Shell
- Spec 12. VM Power-State to Host DPMS Control
- Spec 13. Host Power Button to Guest Power Control
- Spec 14. Proxmox Host Runtime and Bootstrap
