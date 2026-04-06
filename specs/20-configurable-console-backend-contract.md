# Spec 20. Configurable Console Backend Contract

## Context / Problem

Specs 10 through 17 define a working MVP around one console path:

- Proxmox local control through `qm` and `pvesh`
- SPICE connection material written as a `.vv` file
- `remote-viewer` launched from the Cage session

That narrow scope was the right way to reach a usable appliance quickly, but it also left SPICE-specific assumptions in the shared interfaces:

- `console_backend` accepts only `spice`
- runtime config names one SPICE artifact path directly
- IPC uses `connect_spice` instead of a backend-neutral launch contract
- session supervision assumes every console child is `remote-viewer` fed by a `.vv` file

The next spec series wants to add VNC and Looking Glass without breaking the current appliance behavior. The product therefore needs one backend-neutral contract first, so later backend specs can plug into the same daemon, session, waiting-screen, reconnect, and degraded-state model.

## Goals / Non-goals

Goals:

- Preserve the current SPICE experience as the default behavior.
- Define a backend-neutral config, IPC, and runtime-artifact model.
- Allow later specs to add backend-specific settings without reshaping the whole config again.
- Keep one active console child at a time regardless of backend.
- Preserve the existing daemon/session split and failure-policy model from the MVP.

Non-goals:

- Implement VNC-specific console preparation.
- Implement Looking Glass-specific launch or preflight logic.
- Support automatic fallback across multiple backends at runtime.
- Support choosing the console backend from an on-screen UI.
- Support more than one console backend for the same VM at the same time.

## User stories

- As an operator, I can choose the console protocol in `config.toml` instead of being forced onto SPICE.
- As a maintainer, I can add a new console backend without renaming core IPC and state-machine concepts again.
- As a user at the monitor, I still see the same waiting, reconnecting, sleeping, and degraded states regardless of which backend is configured.
- As an operator upgrading an existing SPICE install, I do not have to rewrite the whole config file just because the internal contract became backend-neutral.

## Public API / Interfaces

Primary configuration file remains:

- Path: `/etc/relayinner-display/config.toml`
- Format: TOML

Shared configuration changes:

```toml
[target]
vmid = 101
node_name = "auto"
guest_os = "windows"
console_backend = "spice" # one of: spice, vnc, looking-glass

[runtime]
run_dir = "/run/relayinner-display"
control_socket = "/run/relayinner-display/session.sock"
log_namespace = "relayinner-display"

[console]
artifact_dir = "/run/relayinner-display/console"

[console.spice]
vv_path = "/run/relayinner-display/console/spice-current.vv"
```

Configuration rules:

- `[target].console_backend` expands to `spice`, `vnc`, or `looking-glass`.
- `[console]` becomes the shared namespace for backend-agnostic console settings.
- Backend-specific settings live under fixed tables: `[console.spice]`, `[console.vnc]`, and `[console.looking_glass]`.
- The backend enum value remains `looking-glass` even though the nested config table name uses `looking_glass`.
- `[runtime].spice_vv_path` becomes deprecated but remains accepted only for `console_backend = "spice"` when `[console.spice].vv_path` is omitted.
- If `[console].artifact_dir` is omitted, the default is `/run/relayinner-display/console`.

Daemon-to-session IPC replaces the SPICE-only launch message with a backend-neutral launch contract:

- `{"type":"connect_console","backend":"spice","launcher":"remote-viewer","argv":["remote-viewer","--full-screen","/run/relayinner-display/console/spice-current.vv"]}`
- `{"type":"show_waiting","reason":"vm_stopped"}`
- `{"type":"disconnect_console","reason":"vm_not_running"}`
- `{"type":"display_power","state":"on","output":"HDMI-A-1"}`
- `{"type":"health_ping"}`

Session-to-daemon IPC adds backend identity to console lifecycle messages:

- `{"type":"session_ready"}`
- `{"type":"console_started","backend":"spice","pid":1234}`
- `{"type":"console_exited","backend":"spice","code":1,"signal":0}`
- `{"type":"session_error","reason":"viewer_launch_failed"}`
- `{"type":"display_power_applied","state":"off"}`

Session launch rules:

- The session supervisor validates `backend`, `launcher`, and `argv` before spawning a child.
- The session may execute only an allowlisted launcher for the selected backend.
- MVP-compatible waiting-screen text remains backend-agnostic.
- One active console child remains the only supported model.

Allowlisted launcher mapping:

- `spice` -> `remote-viewer`
- `vnc` -> `remote-viewer`
- `looking-glass` -> `looking-glass-client`

State contract additions:

- runtime state JSON adds `console_backend`
- runtime state JSON adds `active_console_backend`
- degraded reasons that originate in console preparation must include the backend name

Compatibility rules:

- The daemon emits only `connect_console` after this spec is implemented.
- The session must continue accepting `connect_spice` during the transition window so partially upgraded hosts do not fail open during service restart ordering.
- `connect_spice` compatibility can be removed only after Specs 21 and 22 are implemented and the README no longer documents it.

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`

Runtime data:

- `/run/relayinner-display/console/`
- `/run/relayinner-display/session.sock`
- `/run/relayinner-display/daemon.state.json`

Runtime state example:

```json
{
  "vmid": 101,
  "node_name": "pve-01",
  "console_backend": "spice",
  "active_console_backend": "spice",
  "vm_power_state": "running",
  "session_state": "showing_console",
  "last_connect_attempt_at": "2026-04-06T12:00:00Z",
  "last_error": null
}
```

Persistence rules:

- Backend-specific runtime artifacts live under `/run/relayinner-display/console/`.
- Each backend owns only its own files under that directory.
- The daemon removes stale console artifacts for the configured backend on startup.
- Runtime state remains boot-ephemeral and is reconstructed on service restart.

## Security model (Permission/Isolation/Audit)

Permissions:

- The root-owned daemon remains responsible for backend preparation that needs privileged host access.
- The session remains the only process that launches the unprivileged viewer child.
- Backend-specific artifact files must be readable by the session user and writable only by the daemon.

Isolation:

- The backend-neutral IPC contract must not become a generic arbitrary-command runner.
- The session validates the launcher against a static allowlist keyed by backend.
- Only one configured VMID and one configured backend may be controlled.

Audit:

- Log the configured backend at daemon start.
- Log every backend change in config validation output and runtime state.
- Log backend-specific console preparation failures without logging sensitive connection material.
- Include `backend=<name>` in console lifecycle log lines so operators can distinguish SPICE, VNC, and Looking Glass failures quickly.

## Acceptance criteria (Testable, Verifiable)

- Existing SPICE installs still validate and run without user-visible behavior regressions.
- Config parsing accepts `spice`, `vnc`, and `looking-glass` and rejects any other backend name.
- The session can launch a console from `connect_console` and report `console_started` / `console_exited` with backend identity.
- The session rejects a mismatched or non-allowlisted launcher and reports a controlled `session_error`.
- Runtime state and logs identify the configured backend consistently.

## Test plan

Static validation:

- parse legacy SPICE config with only `runtime.spice_vv_path`
- parse new SPICE config with `[console.spice].vv_path`
- reject unsupported `console_backend`
- reject backend-specific config blocks that do not match the selected backend schema

IPC validation:

- accept `connect_console` for each supported backend name
- reject unknown launcher/backend combinations
- verify compatibility handling for legacy `connect_spice`

Session supervision:

- launch one console child from generic `argv`
- replace an existing child when a new `connect_console` arrives
- report backend identity on `console_started` and `console_exited`

Operational checks:

- verify startup cleanup of backend-scoped artifact files
- verify degraded logs include the backend name

## Rollout / Backward compatibility

This spec is additive and must preserve the currently shipped appliance behavior:

- default backend remains `spice`
- config path remains `/etc/relayinner-display/config.toml`
- runtime path remains `/run/relayinner-display`
- service names remain unchanged

Upgrade rules:

- Keep `runtime.spice_vv_path` support for existing installs until the installer and sample config have migrated to `[console.spice].vv_path`.
- The shipped sample config must move to the new `[console]` namespace immediately when this spec lands.
- README and operator docs must state clearly that VNC and Looking Glass are design targets until their backend specs are implemented.

Implementation sequencing:

- Spec 20 is the required convergence point before any parallel backend work starts.
- Finish the shared config contract, generic `connect_console` IPC, backend-neutral session launch path, and SPICE regression coverage in this spec before starting Spec 21 or Spec 22 implementation.

## Open questions

- Whether the final runtime state should keep `active_console_backend` separately from `console_backend` or derive it implicitly.
- Whether the transition compatibility for `connect_spice` is still necessary once installs are always atomically refreshed by `install.sh`.
- Whether future backends will need a backend-specific environment block in IPC, or if curated `argv` alone remains sufficient.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
- Spec 11. Cage Kiosk Session Shell
- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
