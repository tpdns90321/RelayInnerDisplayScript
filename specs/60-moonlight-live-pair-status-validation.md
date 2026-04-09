# Spec 60. Moonlight Live Pair-Status Validation

## Context / Problem

Specs 31 and 40 established the Moonlight pairing workflow and the paired `Desktop` fast-path, but operational evidence exposed a deeper contract bug:

- the relay treated Moonlight's persisted `srvcert` host record as proof that pairing was still valid
- Sunshine's actual pairing truth lives in the live `serverinfo` response field `PairStatus`
- a stale or recreated workspace host record can therefore make the relay skip pairing even when Sunshine would still report the client as unpaired

That mismatch is what makes "delete `state_dir` and try again" unreliable. The workspace is still important, but only as Moonlight's persisted client identity and pinned-host certificate store. Pair truth itself has to come from Sunshine.

## Goals / Non-goals

Goals:

- Define pairing truth as live Sunshine `serverinfo` `PairStatus`, not persisted `srvcert` alone.
- Keep using the managed Moonlight workspace for client identity and pinned-host bootstrap material.
- Preserve the existing `Desktop` fast-path once live pairing has been confirmed.
- Preserve the host/IP-change recovery path when the same Sunshine instance is still paired.
- Distinguish `paired`, `unpaired`, and `unreachable` outcomes clearly in the daemon.

Non-goals:

- Adding new relay config keys for Moonlight pairing.
- Storing Sunshine web-UI usernames or passwords.
- Replacing the existing PIN-assist UI flow with authenticated Sunshine API automation.
- Rewriting Moonlight's internal settings store directly.

## Public API / Interfaces

This spec adds no new config keys.

Live pair-validation contract:

- The daemon first performs the existing TCP reachability probe against the configured Moonlight host authority.
- If the host is reachable, the daemon loads Moonlight client credentials and pinned Sunshine certificate material from the managed workspace.
- The daemon performs a live `serverinfo` probe over HTTP to discover the host's HTTPS port.
- The daemon then performs an authenticated HTTPS `serverinfo` probe using the workspace client certificate, workspace private key, and the pinned Sunshine server certificate.
- `PairStatus = 1` is the only success case that means the relay may treat the configured host as paired.

Workspace contract clarified:

- The managed Moonlight workspace stores client identity and pinned-host certificate material.
- A persisted `srvcert` record by itself is not sufficient proof that pairing is still valid.
- If the workspace lacks the credentials or pinned certificate needed for the live probe, the relay must treat the host as unpaired and fall back to the existing PIN-assist flow.

Host-change recovery contract:

- If the configured host changes but the workspace still contains pinned-host material from an earlier address, the daemon may attempt one live reuse probe using that existing pinned-host material.
- If that probe reports `PairStatus = 1`, the relay treats the configured host as paired and rewrites the managed host record to the configured authority.
- If that probe does not report paired, the relay falls back to a fresh PIN-assist flow for the configured host.

State semantics updated by this spec:

- `moonlight_pair_state = "paired"` means the last authenticated live `serverinfo` probe returned `PairStatus = 1`.
- `moonlight_pair_state = "pending_pin_approval"` means pairing UI is active and the daemon is still waiting for live confirmation.
- `moonlight_pair_state = "unknown"` is used when pairing has not been confirmed yet or when the runtime leaves the pairing flow.

## Data model / Persistence

Persistent data remains:

- `/etc/relayinner-display/config.toml`
- managed Moonlight `state_dir`

Persistent workspace rules:

- `state_dir` still persists Moonlight's own QSettings, client identity, and pinned-host certificate data across restarts.
- The relay still writes only `portable.dat` directly.
- The relay does not persist its own independent "paired" marker under `state_dir`.

Runtime-only data remains:

- `/run/relayinner-display/daemon.state.json`
- active `moonlight_pair_pin` during pending approval

## Security model (Permission/Isolation/Audit)

Permissions:

- Daemon-side pair validation still runs as the relay session user.
- The daemon uses only Moonlight workspace credentials that Moonlight already manages locally.
- No Sunshine web-UI credentials are stored by the relay.

Isolation:

- Pair validation uses Sunshine's `serverinfo` surface only.
- The relay still uses Moonlight CLI for `pair`, `list`, and `stream` where those actions are already part of the backend contract.
- This spec does not add authenticated Sunshine admin API usage.

Audit:

- Log whether the daemon reached a paired, unpaired, or unreachable outcome from the live pair probe.
- Keep journald free of PIN values by default.
- Keep runtime state as the operator-visible source for the current pairing phase.

## Acceptance criteria (Testable, Verifiable)

- A workspace `srvcert` record by itself no longer causes the relay to skip the PIN-assist flow.
- After PIN approval, the relay advances only when the live authenticated `serverinfo` probe reports `PairStatus = 1`.
- With a live-paired `Desktop` host, the relay still skips daemon-side `moonlight list --csv` before launch.
- If the configured host changes but the same Sunshine instance is still paired, the relay recovers that pairing through one live reuse probe before falling back to a new PIN.
- If the live pair probe cannot reach the host, the daemon leaves or stays out of the pairing flow and returns to reconnect behavior instead of falsely marking the host paired.

## Test plan

Daemon behavior:

- workspace contains a pinned host certificate but the live pair probe reports `PairStatus = 0`
- workspace lacks client credentials and the daemon falls back to PIN assist
- live pair probe succeeds after one or more pending polling cycles
- `Desktop` launch skips daemon-side `list` only after live pair confirmation
- host/IP-change recovery succeeds when live pair reuse returns paired

Documentation checks:

- README and ops docs describe the workspace as credential/bootstrap state, not as the final pairing authority
- Spec 31 and Spec 40 point at live `PairStatus` as the pair-truth source

## Rollout / Backward compatibility

This spec is additive at the config layer and narrows behavior only where the previous implementation was overly optimistic:

- existing Moonlight configs remain valid
- healthy pairings continue working
- stale workspace host records no longer short-circuit the PIN-assist flow
- `Desktop` fast-path behavior remains intact once pairing is confirmed live

## Open questions

- Whether a later spec should expose the last live pair-probe timestamp in runtime diagnostics.
- Whether future troubleshooting output should distinguish authentication failure from generic HTTPS connection failure in a separate state field.

## Spec Dependencies

- Spec 31. Moonlight Pair-Assist and Persistent Workspace
- Spec 32. Moonlight Stream Launch, Recovery, and Ops
- Spec 40. Moonlight Desktop Fast-Path and Pair-State Resilience
