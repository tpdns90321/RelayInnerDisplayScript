# Spec 31. Moonlight Pair-Assist and Persistent Workspace

## Context / Problem

Spec 30 adds Moonlight as a backend option, but a usable Moonlight appliance path still needs stateful client identity and a deterministic pairing flow.

Moonlight is not a stateless one-shot viewer like `remote-viewer`:

- it stores client identity and known-host state
- pairing requires a PIN exchange
- Sunshine's documented operator flow approves that PIN in the Sunshine web UI
- the relay must preserve pair state across service restarts without forcing the operator to repeat first-run setup

At the same time, this project should not make Sunshine web-UI credentials part of the relay's stored secret surface in the first iteration. The practical v1 step is therefore a pair-assist model:

- the relay owns the Moonlight workspace
- the relay initiates pairing and shows the PIN
- the operator finishes approval in the Sunshine web UI
- the relay detects completion and continues automatically

## Goals / Non-goals

Goals:

- Persist Moonlight client identity and paired-host state in a relay-managed workspace.
- Initiate Moonlight pairing automatically when the configured Sunshine host is reachable but not yet paired.
- Show the active pairing PIN on the kiosk waiting screen and in runtime state.
- Confirm pairing completion without depending on translated stderr text.
- Preserve successful pair state across restarts.

Non-goals:

- Calling Sunshine's authenticated `/api/pin` endpoint from the relay.
- Storing Sunshine web-UI usernames or passwords in relay config or secrets.
- Rewriting Moonlight's internal QSettings data structure directly.
- Supporting multiple Moonlight workspaces for multiple target guests.
- Providing an on-screen web UI for Sunshine PIN entry.

## User stories

- As an operator, I can boot the appliance, see a pairing PIN, approve it in Sunshine, and continue without manually launching Moonlight.
- As an operator, once pairing succeeds, I do not have to re-pair on every reboot.
- As a maintainer, I can keep pairing support narrow and auditable without adding Sunshine credential storage.
- As a user at the monitor, I see a clear pairing instruction screen instead of a blank or degraded state when only PIN approval is missing.

## Public API / Interfaces

This spec reuses the Moonlight config surface from Spec 30 and adds no new user-facing config keys.

Managed workspace contract:

- The daemon creates `state_dir` on startup if it does not already exist.
- The daemon ensures `state_dir/portable.dat` exists before any Moonlight CLI action.
- All Moonlight CLI commands for this backend, including daemon-side `pair` and `list` checks, execute as the unprivileged relay session user with `cwd = state_dir`.

Host reachability contract:

- Before any pair-assist step, the daemon probes TCP connectivity to the configured Moonlight host authority on `base_port`.
- If the host is unreachable, the relay stays on the existing reconnecting path rather than entering pairing wait.

Pair completion contract:

- Pair completion is defined as a successful `moonlight list <host-authority> --csv` from the managed workspace.
- The relay must not infer pair completion by parsing translated error strings.

Pair-assist flow:

1. If the host is reachable, the daemon runs `moonlight list <host-authority> --csv`.
2. If that command succeeds, the host is treated as paired and the PIN-assist flow is skipped.
3. If that command fails while the host remains reachable, the daemon generates a 4-digit PIN and runs `moonlight pair <host-authority> --pin <pin>`.
4. After the pair command is issued, the daemon enters `waiting_for_pairing` and keeps polling `moonlight list <host-authority> --csv`.
5. When `moonlight list` succeeds, the daemon clears the active PIN and transitions to the ordinary console-launch path.

PIN lifecycle rules:

- A generated PIN is valid only for one pending pairing episode.
- If pairing has not completed within 300 seconds, the daemon generates a new PIN and reissues the `moonlight pair` command.
- The daemon clears the active PIN immediately when pairing completes or the host becomes unreachable.

Session and waiting-screen contract changes:

- runtime `session_state` gains `waiting_for_pairing`
- `show_waiting` gains an optional `details` object

Pairing wait IPC example:

- `{"type":"show_waiting","reason":"pairing_required","details":{"backend":"moonlight","host":"192.168.50.20","pin":"1234","instructions":"Open the Sunshine web UI and enter this PIN."}}`

Runtime state additions:

```json
{
  "console_backend": "moonlight",
  "active_console_backend": null,
  "session_state": "waiting_for_pairing",
  "moonlight_host": "192.168.50.20",
  "moonlight_base_port": 47989,
  "moonlight_pair_state": "pending_pin_approval",
  "moonlight_pair_pin": "1234",
  "last_error": null
}
```

`moonlight_pair_state` values:

- `unknown`
- `pending_pin_approval`
- `paired`

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`
- configured `state_dir`

Persistent workspace rules:

- `state_dir` is created with mode `0700`.
- `state_dir` is owned by the relay session user so Moonlight can update its own QSettings and cache files without root-owned leftovers.
- Moonlight-managed files inside `state_dir` persist across daemon, session, and host restarts unless the operator explicitly removes them.

Relay-managed persistent artifacts in `state_dir`:

- `portable.dat`

Moonlight-managed persistent artifacts in `state_dir`:

- QSettings host records
- Moonlight client identity material
- Moonlight cache files

Runtime-only data:

- `moonlight_pair_pin` lives only in `/run/relayinner-display/daemon.state.json` and in the IPC message sent to the waiting screen.
- The relay must not persist the active PIN under `/var/lib`.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon prepares the workspace and then runs Moonlight CLI helpers as the relay session user.
- No Sunshine web-UI credentials are stored by the relay.
- Pairing PINs are treated as short-lived operational data, not long-lived secrets.

Isolation:

- The relay uses only the curated Moonlight CLI actions required for this flow: `pair` and `list`.
- The relay must not introspect or modify Moonlight's internal settings store beyond creating `portable.dat` and the parent directory.
- The relay must not issue authenticated Sunshine API requests in this spec.

Audit:

- Log transitions into and out of `waiting_for_pairing`.
- Log the configured Sunshine host authority and pairing state transitions.
- Do not log the active PIN by default in journald; the PIN is shown through runtime state and the kiosk waiting view instead.

## Acceptance criteria (Testable, Verifiable)

- On a reachable but unpaired Sunshine host, the relay initiates Moonlight pairing and enters `waiting_for_pairing`.
- The waiting view shows the active PIN and operator instructions.
- After the operator approves the PIN in the Sunshine web UI, the relay detects successful pairing without a service restart.
- After pairing succeeds once, the same host remains paired across later service restarts because the workspace is persistent.
- If the host becomes unreachable during a pending pairing episode, the relay leaves `waiting_for_pairing`, clears the active PIN, and returns to reconnect flow.

## Test plan

Workspace setup:

- create missing `state_dir`
- create missing `portable.dat`
- preserve an existing workspace without destructive rewrite
- verify `state_dir` ownership and mode

Pair-assist behavior:

- host reachable and already paired
- host reachable and not paired
- host unreachable
- pending pairing timeout at 300 seconds
- pairing completes after one or more polling cycles

IPC and state behavior:

- `show_waiting` includes pairing `details`
- runtime state includes `moonlight_pair_state`
- runtime state includes `moonlight_pair_pin` only while pairing is pending

Restart behavior:

- paired workspace survives daemon restart
- restart does not regenerate a PIN when `moonlight list --csv` already succeeds

## Rollout / Backward compatibility

This spec remains additive and backend-scoped:

- SPICE, VNC, and Looking Glass paths remain unchanged.
- Moonlight pair-assist activates only when `console_backend = "moonlight"`.

Documentation rules:

- README and setup docs must describe pairing as `PIN assist`, not fully automatic approval.
- The docs must tell operators exactly where to approve the PIN: Sunshine web UI, `PIN` page, on the guest-side host.
- The docs must state explicitly that Sunshine usernames and passwords are not stored by the relay in this version.

## Open questions

- Whether a later series should add optional authenticated Sunshine API integration for full PIN approval automation.
- Whether the 300-second pairing-attempt window should later become a policy config knob.
- Whether a future host-health probe should include an HTTPS reachability check in addition to the base-port TCP probe.

## Spec Dependencies

- Spec 30. Moonlight Backend Contract and Config
- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
