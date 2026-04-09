# Spec 40. Moonlight Desktop Fast-Path and Pair-State Resilience

## Context / Problem

Specs 31 and 32 together define Moonlight pairing, app validation, and stream launch, but the first integrated implementation exposed a contract gap:

- the relay already had durable Moonlight credentials and pinned-host state in the managed workspace
- the runtime still treated daemon-side `moonlight list --csv` success as a mandatory precondition for every stream launch
- in real deployments, `moonlight list --csv` can hang or time out even when live pairing is still valid and the kiosk-side `moonlight stream Desktop` path is otherwise viable

That mismatch turns a recoverable helper-path failure into a full appliance `degraded` state, even for the default `Desktop` stream that does not need a dynamic Sunshine app-catalog lookup.

The relay therefore needs a narrower, more realistic runtime contract:

- pairing truth must come from live authenticated Sunshine `serverinfo` `PairStatus`
- `Desktop` must be treated as a paired stream surface, not as an app-catalog dependency
- non-`Desktop` apps should keep live app-list validation
- degraded app-validation failures must not erase otherwise valid pair state

## Goals / Non-goals

Goals:

- Decouple pair completion from daemon-side `moonlight list --csv` success.
- Allow paired `Desktop` streams to launch without daemon-side app-list validation.
- Preserve paired Moonlight state when non-pairing failures enter `degraded`.
- Keep non-`Desktop` app launches strict and operator-visible.
- Update docs and operational troubleshooting so the new runtime contract is explicit.

Non-goals:

- Removing live app validation for non-`Desktop` Sunshine apps.
- Adding a new config knob to choose strict versus optimistic launch behavior.
- Automating Sunshine app-catalog discovery through a separate API.
- Introducing a generic retry screen or new UI state just for Moonlight helper failures.
- Changing the pairing UI flow introduced by Spec 31.

## User stories

- As an operator using the default Sunshine `Desktop` entry, I want the relay to start streaming once pairing is complete even if daemon-side `moonlight list` is unstable on my host.
- As an operator using a named Sunshine app such as `Playnite`, I still want a typo or removed app to fail clearly before the kiosk launches a blank or incorrect stream.
- As a maintainer, I want `moonlight_pair_state` to continue reflecting actual pairing state even when the appliance degrades for an app-validation failure.
- As a troubleshooter, I want the docs to tell me whether a Moonlight failure is in pairing, app validation, or stream launch.

## Public API / Interfaces

This spec adds no new config keys and keeps the existing Moonlight config surface:

```toml
[target]
console_backend = "moonlight"

[console.moonlight]
binary = "moonlight"
host = "192.168.50.20"
base_port = 47989
app = "Desktop"
state_dir = "/var/lib/relayinner-display/moonlight"
quit_app_after_session = false
```

Runtime contract changes:

- Pair completion is confirmed from live authenticated Sunshine `serverinfo` `PairStatus`, not from `moonlight list` and not from persisted `srvcert` alone.
- If `app = "Desktop"` and the live pair probe already confirms the configured host as paired, the daemon skips daemon-side `moonlight list --csv` before launch.
- If `app != "Desktop"`, the daemon still runs `moonlight list <host-authority> --csv` and requires a case-insensitive exact app-name match.
- A non-`Desktop` app-list timeout or mismatch may still enter `degraded`, but it must not reset `moonlight_pair_state` from `paired` to `unknown`.

Daemon launch contract:

- Paired `Desktop` launch still emits the same `connect_console` IPC shape from Spec 32.
- Non-`Desktop` launch still emits the same IPC shape after successful live app validation.
- The runtime state file keeps exposing:
  - `moonlight_pair_state`
  - `moonlight_pair_pin`
  - `moonlight_app`

State semantics updated by this spec:

- `moonlight_pair_state = "paired"` means the last live pair probe for the configured host confirmed `PairStatus = 1`.
- `moonlight_pair_state` is cleared only when pairing truly becomes invalid for runtime purposes, such as host unreachable or VM stopped, not when a later app-list helper fails.

## Data model / Persistence

Persistent data remains unchanged:

- `/etc/relayinner-display/config.toml`
- managed Moonlight `state_dir`

Persistent workspace rules clarified:

- The managed workspace keeps Moonlight client identity and pinned-host certificate material under `state_dir`.
- Pair status is derived from live Sunshine `serverinfo` responses bootstrapped by that workspace material.
- The relay does not persist its own independent pair-state marker.
- The relay does not persist a cached Sunshine app catalog.
- A persisted `srvcert` host record alone is never sufficient to mark the host paired.

Runtime-only data remains:

- `/run/relayinner-display/daemon.state.json`
- `/run/relayinner-display/session.sock`

Runtime persistence rules added by this spec:

- `moonlight_pair_state` remains `paired` across app-validation degradation when the last live pair probe still confirms pairing.
- `moonlight_pair_pin` is still cleared when pairing succeeds, when the host becomes unreachable, or when the VM stops.

## Security model (Permission/Isolation/Audit)

Permissions:

- Daemon-side Moonlight helpers still run as the relay session user against the managed workspace.
- The session still launches Moonlight directly as the relay session user.

Isolation:

- The relay continues to use only the curated Moonlight CLI surface already approved for this backend: `pair`, `list`, and `stream`.
- This spec does not add Sunshine credentials, API tokens, or direct manipulation of Sunshine config.

Audit:

- Log when the daemon skips app-list validation for a live-paired `Desktop` stream.
- Log successful live validation for non-`Desktop` apps.
- Keep degraded reasons concise and backend-tagged for non-`Desktop` list timeouts or app mismatches.
- Do not log PINs in journald by default.

## Acceptance criteria (Testable, Verifiable)

- With a live-paired host and `app = "Desktop"`, the relay launches Moonlight even if daemon-side `moonlight list --csv` would otherwise time out.
- With a live-paired host and a non-`Desktop` app, the relay still validates against the live app list before launch.
- If that non-`Desktop` app validation fails or times out, the relay enters `degraded` with a clear reason and keeps `moonlight_pair_state = "paired"`.
- If the host becomes unreachable during a pending pairing episode, the relay still clears the PIN and returns to reconnect flow.
- Existing SPICE, VNC, Looking Glass, and Moonlight pairing UI behavior remain unchanged.

## Test plan

Daemon behavior:

- live-paired `Desktop` app with no daemon-side `list` call
- live-paired non-`Desktop` app with successful live list validation
- live-paired non-`Desktop` app mismatch
- live-paired non-`Desktop` list timeout
- live-paired host plus unexpected Moonlight exit and reconnect

State behavior:

- degraded non-`Desktop` validation preserves `moonlight_pair_state = "paired"`
- host unreachable still clears pending pairing state
- VM stopped still clears pending pairing state

Documentation checks:

- sample config comment describes the `Desktop` fast-path accurately
- README and setup docs distinguish `Desktop` launch from non-`Desktop` app validation
- spec index includes Spec 40

Manual checks:

- paired `Desktop` kiosk launch on a host where daemon-side `moonlight list` is known to hang
- paired non-`Desktop` launch with a valid Sunshine app
- paired non-`Desktop` launch with an intentionally invalid app name

## Rollout / Backward compatibility

This spec is additive and backward-compatible at the config layer:

- no config migration is required
- existing `Desktop` users become less fragile without changing config
- existing non-`Desktop` users keep the same strict validation behavior

Operational compatibility notes:

- troubleshooting now depends on whether `app = "Desktop"` or not
- operators should use `moonlight_pair_state` in the runtime state file to distinguish pairing loss from app-validation failure

Documentation rules:

- README and ops docs must describe pair state as live `PairStatus`-confirmed, with the workspace used only for Moonlight credentials and pinned-host bootstrap data
- README and ops docs must explicitly call out the `Desktop` fast-path
- docs must preserve the warning that `quit_app_after_session = true` is invalid for `Desktop`

## Open questions

- Whether a later spec should add a dedicated runtime state field for the last successful Moonlight app-list validation timestamp.
- Whether a future spec should optionally allow optimistic launch for selected non-`Desktop` apps after one prior successful validation.
- Whether Moonlight helper diagnostics should surface a dedicated troubleshooting hint when `QT_QPA_PLATFORM=offscreen` still hangs on a given host.

## Spec Dependencies

- Spec 30. Moonlight Backend Contract and Config
- Spec 31. Moonlight Pair-Assist and Persistent Workspace
- Spec 32. Moonlight Stream Launch, Recovery, and Ops
- Spec 15. MVP Integration, Failure Policy, and Ops
