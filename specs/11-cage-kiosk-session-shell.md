# Spec 11. Cage Kiosk Session Shell

## Context / Problem

The relay appliance must not expose the normal Proxmox host console, a login prompt, or a multi-window desktop. The attached display is intended to behave like a dedicated terminal for one guest VM, so the local session model must be as constrained as the relay logic itself.

At the same time, the display side still needs a small amount of local orchestration:

- a waiting screen before the VM is available
- a launcher for `remote-viewer`
- child-process supervision and crash reporting
- a place to apply Wayland display power actions from later specs

Using Cage provides a single-application kiosk compositor, but the MVP still needs a concrete process model, unit structure, and crash behavior.

## Goals / Non-goals

Goals:

- Boot directly into a single-purpose Cage session on a fixed TTY.
- Run one session supervisor as the only long-lived application inside Cage.
- Show an intentional waiting screen when the guest is not connected.
- Recover from `remote-viewer`, session, or Cage failure without leaving a shell prompt.

Non-goals:

- General-purpose desktop access
- User login workflows
- Rich on-screen configuration UI
- Multi-window local applications
- Remote desktop management of the host session

## User stories

- As an operator, I can boot the host and reach the relay display without entering credentials.
- As a user, I cannot accidentally escape from the guest relay into the Proxmox host environment.
- As an operator, if `remote-viewer` crashes, the screen returns to a known waiting or reconnecting state.
- As an operator, if Cage exits, systemd restarts the display stack automatically.

## Public API / Interfaces

Systemd units:

- `relayinner-display-seatd.service`
- `relayinner-display-kiosk.service`
- `relayinner-displayd.service`

User and seat model:

- system user: `relayinner-display`
- fixed TTY: `/dev/tty1`
- session launcher: `cage`, connected to `relayinner-display-seatd.service`

Display service command:

```text
cage -- /usr/local/lib/relayinner-display/session-entrypoint
```

Host session policy:

- The installation disables any conflicting graphical login manager on `tty1`.
- The installation masks or overrides `getty@tty1.service` so the relay session owns that VT.
- No shell is presented on normal or failure paths.

Session supervisor responsibilities:

- render a full-screen waiting view when no console is active
- spawn `remote-viewer` as a child process when instructed by the daemon
- kill or replace the child when the daemon sends `disconnect_console`
- report child exit or launch failure back to the daemon
- apply display-power commands from within the Wayland session

Daemon-to-session messages introduced or reused:

- `{"type":"show_waiting","reason":"vm_stopped"}`
- `{"type":"show_waiting","reason":"reconnecting"}`
- `{"type":"connect_spice","vv_path":"/run/relayinner-display/current.vv"}`
- `{"type":"disconnect_console","reason":"vm_not_running"}`
- `{"type":"display_power","state":"on","output":"HDMI-A-1"}`

Session-to-daemon messages:

- `{"type":"session_ready"}`
- `{"type":"console_started","pid":1234}`
- `{"type":"console_exited","code":1,"signal":0}`
- `{"type":"display_power_applied","state":"off"}`

Visual contract:

- background: static dark background with centered text
- status text values: `Connecting`, `Waiting for VM`, `Connection lost`, `Display sleeping`, `Degraded`
- cursor hidden while `remote-viewer` is active

## Data model / Persistence

Persistent assets:

- session launcher script under `/usr/local/lib/relayinner-display/`
- optional background asset under `/usr/local/share/relayinner-display/`
- systemd units under `/etc/systemd/system/`

Runtime data:

- Wayland socket exported to the session process
- PID of the active `remote-viewer` child
- current on-screen state kept in process memory

No user-generated state is persisted. The session reconstructs its state from daemon messages after each service restart.

## Security model (Permission/Isolation/Audit)

Permissions:

- `relayinner-display-session` runs as a non-login account with no interactive shell.
- Device access is granted only through the seatd-managed session.

Isolation:

- No terminal emulator, launcher, or shell binary is reachable from the on-screen session.
- The session sanitizes environment variables passed to `remote-viewer`.
- MVP does not bind any escape shortcuts to open a shell or another application.

Audit:

- Log Cage start and exit.
- Log session supervisor start, child launch, child crash, and unexpected restarts.
- Tag logs so operators can distinguish session failures from Proxmox control failures.

## Acceptance criteria (Testable, Verifiable)

- After boot, the host reaches Cage on `tty1` automatically and shows the waiting screen if no VM is available.
- No desktop shell, login manager, or alternate application window appears on the display.
- If `remote-viewer` crashes, the session returns to a managed waiting or reconnecting state without exposing a host prompt.
- If Cage exits unexpectedly, `relayinner-display-kiosk.service` restarts it according to systemd policy.
- Display power commands from later specs can be applied successfully from the session context.

## Test plan

Boot-flow tests:

- Fresh boot with target VM stopped
- Fresh boot with target VM already running
- Reboot while `remote-viewer` is active

Crash-recovery tests:

- Kill `remote-viewer`
- Kill `relayinner-display-session`
- Kill Cage

Containment checks:

- Verify no shell appears after repeated failures
- Verify cursor visibility policy
- Verify `tty1` ownership remains with the relay stack

## Rollout / Backward compatibility

This spec defines the final kiosk topology for MVP:

- one Cage session
- one session supervisor
- one active `remote-viewer` process at a time

Later visual changes may replace the waiting-screen presentation, but they must keep the single-application kiosk model and avoid reintroducing a general desktop or login screen.

## Open questions

- Whether additional systemd VT settings are needed on Proxmox hosts that ship with different console defaults.
- Whether hiding the cursor needs toolkit-specific handling beyond Cage defaults.
- Whether the waiting screen should eventually show a minimal reason code or only generic text.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
