# Spec 12. VM Power-State to Host DPMS Control

## Context / Problem

The monitor attached to the Proxmox host should behave like an extension of the guest VM rather than a general-purpose host display. If the guest is off, the monitor should enter standby. If the guest is active, the monitor should wake and show the relay session. The implementation must avoid obvious user-facing glitches such as rapid on/off transitions during short-lived command failures or transient VM state changes.

Because Cage owns the display session on Wayland, the physical output power operation should happen inside the session context. The policy decision, however, belongs in the root-owned control daemon that already tracks VM power state.

## Goals / Non-goals

Goals:

- Map guest power state to host monitor power intent.
- Wake the display immediately when the guest becomes active.
- Delay display-off transitions enough to avoid visible flapping.
- Keep display-power failure non-fatal to the rest of the relay appliance.

Non-goals:

- Suspending or hibernating the Proxmox host
- HDMI-CEC or TV power control
- Guest idle detection
- Multi-monitor layout or per-seat routing

## User stories

- As an operator, when the VM is off, the attached monitor should enter standby automatically.
- As an operator, when the VM boots, the monitor should wake without extra intervention.
- As an operator, a short `qm` polling failure should not cause visible display flicker.
- As a user, I should not face a sleeping screen while the guest is actively running.

## Public API / Interfaces

Config additions in `/etc/relayinner-display/config.toml`:

```toml
[display]
output_name = "HDMI-A-1"
power_helper = "wlopm"

[policy]
dpms_policy = "vm-power"
dpms_off_delay_ms = 5000
power_state_stabilize_ms = 3000
```

VM power interpretation:

- `running` -> intent `display_on`
- `paused` -> intent `display_on`
- `stopped` -> intent `display_off`
- `shutdown` -> intent `display_off`
- `suspended` -> intent `display_on`
- `starting` or `stopping` -> preserve current display state
- `unknown` or command failure -> preserve current display state

Daemon-to-session messages:

- `{"type":"display_power","state":"on","output":"HDMI-A-1"}`
- `{"type":"display_power","state":"off","output":"HDMI-A-1"}`

Session execution contract:

- Default helper is `wlopm`.
- The helper is executed from the Wayland session environment.
- If `output_name` is empty, apply the action to all visible outputs.

Debounce rules:

- A transition to `display_off` is emitted only after `dpms_off_delay_ms` while the VM remains off.
- A transition to `display_on` is emitted immediately once the VM reaches `running` or `paused`.
- `power_state_stabilize_ms` applies before acting on a new off-state after startup.
- Polling failures alone do not change display power.

## Data model / Persistence

Additional runtime state in `/run/relayinner-display/daemon.state.json`:

```json
{
  "vm_power_state": "stopped",
  "display_power_intent": "off",
  "display_power_applied": "off",
  "power_state_since": "2026-04-03T12:00:00Z"
}
```

Persistence rules:

- Display state is runtime-only and is not restored from disk on boot.
- On startup, the daemon assumes `display_on` until the first stable VM-state evaluation completes.
- The session reapplies the most recent power intent after it reconnects to the daemon.

## Security model (Permission/Isolation/Audit)

Permissions:

- The root daemon decides power policy.
- The session supervisor performs output-power commands inside the Wayland session.

Isolation:

- IPC accepts only explicit `on` and `off` power states.
- The session rejects malformed or unknown power commands.
- No arbitrary shell fragments are accepted over IPC.

Audit:

- Log every display-power intent transition with the triggering VM state.
- Log helper command failures with output name and exit status.
- Suppress repetitive no-change log spam during stable polling.

## Acceptance criteria (Testable, Verifiable)

- If the VM transitions from `stopped` to `running`, the display wakes within one polling interval plus one second.
- If the VM transitions from `running` to `stopped`, the display enters standby only after `dpms_off_delay_ms`.
- If the VM is `paused`, `starting`, or temporarily `unknown`, the display does not power off solely because of that state.
- If the output-power helper fails, the relay stays otherwise functional and the failure is logged clearly.
- During a transient Proxmox command failure while the VM remains active, the display stays on.

## Test plan

Functional tests:

- Boot with VM stopped and verify delayed display off
- Start VM and verify immediate display on
- Stop VM cleanly and verify delayed display off
- Pause VM and verify display remains on

Failure tests:

- Force `qm status` failure and verify no display flapping
- Remove or break the power helper and verify logged failure
- Restart the session while the display is off and verify reapplication

Manual checks:

- Confirm the waiting-screen state and actual display-power state stay coherent
- Confirm the configured `output_name` matches the intended physical monitor

## Rollout / Backward compatibility

This spec extends the shared config file with `[display]` and new `[policy]` keys. Deployments that omit them should default to:

- `dpms_policy = "vm-power"`
- `dpms_off_delay_ms = 5000`
- `power_state_stabilize_ms = 3000`

Future implementations may support alternative Wayland output-power helpers, but they must preserve the same `display_on` and `display_off` semantics for compatibility.

## Open questions

- Whether `wlopm` behaves consistently enough across the Proxmox host environments targeted for MVP.
- Whether some monitors require a second wake attempt after a long standby interval.
- Whether `paused` should remain display-on for all operator profiles or become configurable later.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
- Spec 11. Cage Kiosk Session Shell
