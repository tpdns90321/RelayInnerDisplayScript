# Spec 32. Moonlight Stream Launch, Recovery, and Ops

## Context / Problem

Specs 30 and 31 define backend selection, a managed Moonlight workspace, and the pair-assist path. The remaining gap is the actual runtime appliance behavior once pairing is complete:

- validate that the requested Sunshine app exists
- launch Moonlight in deterministic fullscreen appliance mode
- reconnect cleanly when Moonlight exits unexpectedly
- keep waiting, reconnecting, degraded, and display-sleep behavior aligned with the existing relay state machine
- document the operator contract clearly enough that Moonlight does not become a hidden side path

Without those rules, the backend would exist in config but remain operationally ambiguous.

## Goals / Non-goals

Goals:

- Launch a configured Sunshine app automatically with `moonlight stream`.
- Preserve the existing single-child kiosk supervision and reconnect model.
- Validate app existence before launch so misconfigured app names fail clearly.
- Keep Moonlight backend docs, sample config, and dependency validation aligned.
- Define the smallest supportable Moonlight runtime path for this appliance.

Non-goals:

- Showing Moonlight's app picker or other interactive Moonlight UI before streaming.
- Managing Sunshine's app catalog or editing Sunshine's `apps.json`.
- Exposing the full Moonlight command-line option set in relay config.
- Supporting multiple simultaneous streams, multiple guests, or multiple Moonlight profiles.
- Replacing the current Proxmox-SPICE default workflow.

## User stories

- As an operator, I can select `Desktop` or another known Sunshine app and have the relay start it automatically.
- As an operator, if I typo the app name, I get a clear degraded reason instead of a blank fullscreen client.
- As an operator, if Moonlight crashes while the VM is still running, the relay reconnects automatically.
- As a user at the monitor, I still see the same appliance lifecycle as the other backends: waiting, reconnecting, showing, sleeping, or degraded.

## Public API / Interfaces

This spec reuses the Moonlight config from Spec 30:

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

Config rules finalized in this spec:

- `app` comparison is case-insensitive against the Moonlight app list.
- `quit_app_after_session = true` is invalid when `app = "Desktop"` because the Desktop entry is treated as a stream-only surface rather than a relay-managed launchable program.

App validation contract:

- Before launch, the daemon skips app-list validation when `app = "Desktop"` and pairing has already been confirmed from the managed workspace.
- Before launch for any non-`Desktop` app, the daemon runs `moonlight list <host-authority> --csv` from the managed workspace.
- For those non-`Desktop` apps, the daemon parses the CSV output and requires a case-insensitive exact app-name match.
- If the configured app does not exist, the relay enters `degraded` with a backend-tagged reason.

Moonlight launch contract:

- The daemon-to-session launch message is:

```json
{
  "type": "connect_console",
  "backend": "moonlight",
  "launcher": "moonlight",
  "cwd": "/var/lib/relayinner-display/moonlight",
  "argv": [
    "moonlight",
    "stream",
    "192.168.50.20",
    "Desktop",
    "--display-mode",
    "fullscreen"
  ]
}
```

Launch rules:

- The relay always passes `--display-mode fullscreen`.
- The relay appends `--quit-after` only when `quit_app_after_session = true`.
- The relay does not pass additional Moonlight tuning flags in this first runtime spec.
- The relay launches Moonlight only after:
  - the VM is running
  - the Sunshine host is reachable on `base_port`
  - pairing is confirmed per Spec 31
  - the configured app exists in the current Moonlight app list

Daemon runtime behavior:

- VM stopped -> `show_waiting`
- VM running, host unreachable -> reconnect flow with bounded backoff
- VM running, host reachable, app missing -> `degraded`
- VM running, host reachable, paired, app valid -> `connect_console`

Session behavior:

- The session launches one Moonlight child at a time.
- Unexpected exit reports `{"type":"console_exited","backend":"moonlight","code":...,"signal":...}`.
- Intentional disconnect because the VM stopped suppresses the exit event exactly like the existing backends.

Runtime state additions:

```json
{
  "console_backend": "moonlight",
  "active_console_backend": "moonlight",
  "session_state": "showing_console",
  "moonlight_host": "192.168.50.20",
  "moonlight_base_port": 47989,
  "moonlight_pair_state": "paired",
  "moonlight_pair_pin": null,
  "moonlight_app": "Desktop",
  "last_error": null
}
```

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`
- managed Moonlight `state_dir`

Runtime data:

- `/run/relayinner-display/daemon.state.json`
- `/run/relayinner-display/session.sock`

Persistence rules:

- App-list validation for non-`Desktop` apps is always derived from live `moonlight list --csv` output; the relay does not persist its own copy of the Sunshine app catalog.
- The relay continues to reuse the persistent Moonlight workspace from Spec 31.
- `moonlight_app` in runtime state is operational metadata only.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon continues to run Moonlight helper checks as the relay session user so the persistent workspace stays single-owner.
- The session launches the Moonlight stream child as the relay session user.

Isolation:

- The session must spawn Moonlight directly, not through a shell.
- The daemon uses only the curated CLI actions required by this series: `list`, `pair`, and `stream`.
- The relay does not store Sunshine credentials and does not modify Sunshine application definitions.

Audit:

- Log `backend=moonlight app=<name>` when preparing a launch.
- Log a concise degraded reason when the app is missing.
- Log Moonlight child launch and exit the same way the existing backends log viewer lifecycle events.

## Acceptance criteria (Testable, Verifiable)

- With a paired Moonlight workspace and a valid Sunshine app, the relay launches Moonlight fullscreen inside the kiosk session.
- If the configured app name does not exist in the Sunshine app list, the relay enters `degraded` with a clear backend-tagged reason.
- If Moonlight exits unexpectedly while the VM is still running and the host remains reachable, the relay re-enters reconnect flow and relaunches it according to the shared backoff policy.
- If the VM stops, the relay disconnects the Moonlight child and returns to the ordinary waiting/display-sleep path.
- If `quit_app_after_session = true` and `app = "Desktop"`, config validation fails before runtime.

## Test plan

Config validation:

- accept `quit_app_after_session = false` with `app = "Desktop"`
- reject `quit_app_after_session = true` with `app = "Desktop"`
- accept a non-Desktop app with `quit_app_after_session = true`

Daemon behavior:

- paired host plus valid app
- paired host plus missing app
- paired host unreachable
- VM stopped
- unexpected Moonlight exit while VM stays running

Session behavior:

- verify fullscreen CLI construction
- verify `--quit-after` is appended only when enabled
- verify backend-tagged `console_exited` on unexpected termination

Manual checks:

- confirm the kiosk returns directly into a fullscreen Moonlight session
- confirm a typo in `app` produces a controlled degraded screen instead of a blank stream
- confirm a forced Moonlight crash or kill transitions into reconnect flow

## Rollout / Backward compatibility

This spec is additive and opt-in:

- existing backends remain unchanged
- Moonlight remains inactive unless selected in config

Documentation rules:

- README and sample config must document the Moonlight backend as Sunshine-only support.
- The docs must state that operators must install Moonlight on the Proxmox host themselves.
- The docs must state that Sunshine app definitions such as `Desktop` remain guest-managed.

Operational dependency rules:

- The runtime dependency list must include Moonlight when `console_backend = "moonlight"`.
- The docs must state the minimum supported Moonlight version as `6.0.0`.

## Open questions

- Whether a later series should add a curated Moonlight tuning surface for bitrate, resolution, FPS, and HDR.
- Whether a future spec should surface a dedicated troubleshooting screen for app-list mismatch instead of the generic degraded state.
- Whether a later series should allow a relay-managed non-default Moonlight profile directory per backend instance.

## Spec Dependencies

- Spec 30. Moonlight Backend Contract and Config
- Spec 31. Moonlight Pair-Assist and Persistent Workspace
- Spec 15. MVP Integration, Failure Policy, and Ops
