# Spec 50. Kiosk Compositor Selection Contract

## Context / Problem

Specs 11 and 14 hard-code the appliance kiosk around `cage`:

- the installed kiosk unit launches `cage -- /usr/local/lib/relayinner-display/session-entrypoint`
- the session supervisor and console backends all inherit that one compositor choice
- the runtime has no way to express that one backend may need a different compositor contract than another

That was acceptable while the relay only targeted the original SPICE path and the later VNC and Looking Glass follow-ups. The Moonlight series changed the risk profile:

- Moonlight still fits the relay's one-child appliance model
- but field testing showed a compositor-specific output-detection difference between `cage` and `sway`
- the project now needs a supported way to choose the kiosk compositor without reopening the backend-neutral session and daemon contracts again

The relay therefore needs an explicit kiosk-compositor contract:

- compositor choice must become configuration, not a launcher constant
- existing non-Moonlight backends must keep their current behavior by default
- Moonlight must be able to follow a different supported kiosk path
- unsupported compositor and backend combinations must fail clearly instead of degrading later in session startup

## Goals / Non-goals

Goals:

- Add an explicit kiosk-compositor configuration surface.
- Keep existing service names, daemon/session IPC, and appliance ownership model unchanged.
- Resolve a default compositor automatically from the selected console backend.
- Expose the resolved compositor in runtime diagnostics.
- Reject unsupported backend and compositor combinations early and clearly.

Non-goals:

- Supporting arbitrary compositor commands from config.
- Creating a general compositor plugin system in this series.
- Replacing the existing daemon/session split.
- Adding runtime hot-switching between compositors without a service restart.
- Defining support for non-Moonlight `sway` operation in this spec.

## User stories

- As an operator using SPICE, VNC, or Looking Glass, I want the relay to keep the current `cage` path unless I intentionally opt into later work.
- As an operator using Moonlight, I want the relay to follow a supported compositor path without rewriting systemd units manually.
- As a maintainer, I want compositor choice to live in one explicit contract rather than being buried inside launcher code.
- As a troubleshooter, I want runtime state and journald to tell me which compositor the kiosk actually resolved to.

## Public API / Interfaces

Primary configuration file remains:

- Path: `/etc/relayinner-display/config.toml`
- Format: TOML

This spec adds a new optional kiosk table:

```toml
[kiosk]
compositor = "auto"
```

Allowed values:

- `auto`
- `cage`
- `sway`

Resolution rules:

- If `compositor = "auto"` and `console_backend = "moonlight"`, the resolved compositor is `sway`.
- If `compositor = "auto"` and `console_backend` is `spice`, `vnc`, or `looking-glass`, the resolved compositor is `cage`.
- If `compositor = "cage"`, it is valid only for `spice`, `vnc`, and `looking-glass`.
- If `compositor = "sway"`, it is valid only for `moonlight`.

Invalid combinations:

- `console_backend = "moonlight"` with `compositor = "cage"` is invalid.
- `console_backend = "spice"`, `vnc`, or `looking-glass` with `compositor = "sway"` is invalid.
- Invalid combinations must be rejected during config validation with a clear error string that names both the backend and the unsupported compositor.

Managed launcher contract changes:

- The installed kiosk unit keeps the existing service name `relayinner-display-kiosk.service`.
- The unit no longer embeds a compositor command directly.
- The unit launches one relay-managed kiosk launcher binary at `/usr/local/lib/relayinner-display/relayinner-display-kiosk`.
- That launcher reads the config, resolves the compositor, and `exec`s the compositor-specific command without a shell wrapper.

Resolved compositor command shapes:

- `cage -- /usr/local/lib/relayinner-display/session-entrypoint`
- `sway --config /run/relayinner-display/sway.config`

Runtime state contract additions:

- `/run/relayinner-display/daemon.state.json` adds `kiosk_compositor`.
- `kiosk_compositor` contains the resolved compositor value, not the raw configured value.

Existing daemon-to-session IPC remains unchanged in this spec.

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`

Persistent config rules:

- `[kiosk]` is optional.
- If `[kiosk]` is omitted, `compositor = "auto"` is assumed.

Runtime data:

- `/run/relayinner-display/daemon.state.json`
- `/run/relayinner-display/session.sock`
- compositor-specific runtime artifacts under `/run/relayinner-display/`

Runtime persistence rules:

- `kiosk_compositor` is written into runtime state after config resolution succeeds.
- Compositor-specific generated files remain runtime-only and are not persisted across reboot.

## Security model (Permission/Isolation/Audit)

Permissions:

- The kiosk compositor still runs as the unprivileged `relayinner-display` user.
- The root-owned daemon remains separate from the compositor launcher path.

Isolation:

- The relay accepts only the curated compositor allowlist `cage` or `sway`.
- The kiosk launcher must not pass through arbitrary extra compositor flags from config.
- The kiosk launcher must not use a shell to invoke the selected compositor.

Audit:

- Log the configured kiosk compositor value and the resolved compositor value at kiosk startup.
- Log unsupported backend and compositor combinations as explicit startup failures.
- Keep service names and subsystem logging namespace unchanged so operators can reuse existing journal workflows.

## Acceptance criteria (Testable, Verifiable)

- Existing configs without `[kiosk]` remain valid.
- `console_backend = "moonlight"` resolves to `kiosk_compositor = "sway"` when `[kiosk]` is omitted.
- `console_backend = "spice"`, `vnc`, and `looking-glass` resolve to `kiosk_compositor = "cage"` when `[kiosk]` is omitted.
- `moonlight + cage` is rejected during config validation.
- `spice|vnc|looking-glass + sway` is rejected during config validation.
- The installed kiosk unit launches the relay-managed kiosk launcher instead of embedding `cage` directly.
- Runtime state exposes `kiosk_compositor` once startup validation has succeeded.

## Test plan

Config validation:

- accept omitted `[kiosk]`
- accept `[kiosk] compositor = "auto"`
- accept `moonlight + sway`
- accept `spice + cage`
- reject `moonlight + cage`
- reject `spice + sway`
- reject unknown compositor values

Launcher behavior:

- verify kiosk command generation uses `/usr/local/lib/relayinner-display/relayinner-display-kiosk`
- verify resolved `cage` command shape
- verify resolved `sway` command shape
- verify the launcher uses `exec` semantics without a shell

Runtime state:

- verify `kiosk_compositor` is present for valid resolved startup

Regression checks:

- existing daemon/session IPC fixtures remain unchanged
- existing non-Moonlight backends keep the same resolved kiosk path under `auto`

## Rollout / Backward compatibility

This spec is additive at the config layer:

- existing installs do not need a config migration
- non-Moonlight operators keep the current `cage` behavior under the default `auto` path
- Moonlight gains a supported default compositor path without requiring manual unit edits

Operational compatibility rules:

- documentation must distinguish the raw configured value from the resolved runtime compositor
- service names and unit ownership remain unchanged

## Open questions

- Whether a later spec should support `sway` for non-Moonlight backends after separate validation.
- Whether a future series should expose a small curated compositor-tuning surface such as output placement or startup environment overrides.
- Whether the kiosk launcher should eventually expose the configured value in runtime state alongside the resolved value.

## Spec Dependencies

- Spec 11. Cage Kiosk Session Shell
- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
- Spec 20. Configurable Console Backend Contract
- Spec 30. Moonlight Backend Contract and Config
