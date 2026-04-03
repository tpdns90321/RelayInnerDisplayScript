# Spec 13. Host Power Button to Guest Power Control

## Context / Problem

The appliance uses the host chassis as an input surface, so the physical power button should control the guest rather than shutting down the Proxmox host itself. The required behavior is state-dependent:

- if the target VM is off, pressing the button should start it
- if the target VM is on, pressing the button should request a graceful shutdown

That behavior must be implemented carefully. Two failure classes matter most for MVP:

- the host powers off because its default logind policy still owns the button
- repeated button presses enqueue duplicate VM actions

Because this runs directly on the Proxmox host, the implementation can capture the local evdev power-button event and call `qm start` or `qm shutdown` locally.

## Goals / Non-goals

Goals:

- Capture the physical host power-button press event.
- Override normal host shutdown behavior for that button.
- Start the guest when it is stopped.
- Request graceful guest shutdown when it is running.
- Debounce repeated presses and suppress conflicting in-flight actions.

Non-goals:

- Forwarding raw ACPI payloads into the guest
- Sleep-button, lid-switch, or reset-button handling
- Force-stop semantics in MVP
- Long-press alternate actions

## User stories

- As a user standing at the appliance, if the VM is off, one power-button press should boot it.
- As a user, if the VM is on, one power-button press should request guest shutdown.
- As an operator, the Proxmox host should stay up regardless of that button press.
- As an operator, repeated fast presses should not queue multiple contradictory VM actions.

## Public API / Interfaces

Config additions in `/etc/relayinner-display/config.toml`:

```toml
[input]
power_button_event = "/dev/input/by-path/platform-i8042-serio-0-event-power"
forward_power_button = true
debounce_ms = 2000

[policy]
power_button_action_when_running = "shutdown"
power_button_action_when_stopped = "start"
shutdown_timeout_s = 90
```

Button-capture model:

- `relayinner-displayd` opens the configured evdev node read-only.
- It listens only for `KEY_POWER` press events.
- Release events are ignored.

Host policy contract:

- Installation writes a `logind.conf.d` override so the host does not power off on a power-button press.
- The daemon performs a startup validation check that host power-key handling is disabled or explicitly ignored.
- If host policy override is missing, the daemon enters `degraded` and refuses to own the button path.

Guest action contract:

- If VM state is `stopped` or `shutdown`, run `qm start <vmid>`.
- If VM state is `running` or `paused`, run `qm shutdown <vmid>`.
- If VM state is `starting`, `stopping`, or `unknown`, ignore the button press and log it as non-actionable.

Rate-limiting rules:

- Ignore any additional accepted press within `debounce_ms` of the previous accepted press.
- Maintain at most one in-flight power-button action at a time.
- While an action is in flight, additional presses are logged and discarded.

## Data model / Persistence

Additional runtime state:

```json
{
  "last_power_button_at": "2026-04-03T12:00:00Z",
  "power_button_action_in_flight": true,
  "last_power_button_action": "start",
  "last_power_button_result": "submitted"
}
```

Persistence rules:

- Button state is runtime-only.
- No historical event database is required in MVP.
- Journald is the audit trail for accepted, ignored, and failed button actions.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon requires read access to one evdev power-button device.
- The daemon requires permission to invoke local `qm start` and `qm shutdown` for the configured VM.

Isolation:

- Only the configured evdev node is opened.
- Only `KEY_POWER` press events are acted upon.
- Only the configured VMID may be started or shut down through this path.

Audit:

- Log every accepted button press.
- Log whether the action chosen was `start`, `shutdown`, or `ignored`.
- Log startup validation failure if host power-button policy still belongs to logind.
- Never log secrets; only log VMID, action, and result.

## Acceptance criteria (Testable, Verifiable)

- A single power-button press while the VM is stopped causes exactly one `qm start` request for the configured VM.
- A single power-button press while the VM is running causes exactly one `qm shutdown` request for the configured VM.
- The Proxmox host remains running after the button press.
- Rapid repeated presses within `debounce_ms` do not produce duplicate guest actions.
- If the VM is in a transitional state, no conflicting action is sent and the event is logged clearly.

## Test plan

Functional tests:

- Press the power button once while the VM is stopped
- Press the power button once while the VM is running
- Press the power button rapidly three times

Failure tests:

- Remove access to the configured evdev node and verify degraded startup
- Leave host power-button policy enabled and verify fail-fast behavior
- Force `qm shutdown` failure and verify logged error without daemon crash

Manual checks:

- Confirm the guest starts from the stopped state
- Confirm the guest begins its normal shutdown path from the running state
- Confirm the host stays online after each action

## Rollout / Backward compatibility

This spec adds optional `[input]` and `[policy]` keys. If `forward_power_button = false`, the daemon does not open the evdev node and this feature remains disabled.

Future versions may add long-press behavior or alternate mappings, but the default single-press semantics defined here must remain stable for MVP compatibility:

- stopped VM -> start
- running VM -> graceful shutdown

## Open questions

- Whether the power-button evdev path is stable enough across all target hardware or should be resolved through udev attributes at runtime.
- Whether `qm shutdown` timeout or guest-agent presence should influence user-visible status text during shutdown.
- Whether `paused` should map to shutdown or require explicit resume-first handling in later versions.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
