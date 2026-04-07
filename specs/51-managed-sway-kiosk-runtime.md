# Spec 51. Managed Sway Kiosk Runtime

## Context / Problem

Spec 50 separates kiosk compositor choice from the hard-coded `cage` launcher, but that contract alone is not enough to ship `sway` as an appliance runtime path.

The relay still needs one managed, reproducible, non-desktop `sway` environment:

- operators must not have to hand-write or maintain a `sway` config
- the runtime must still boot into one fullscreen appliance session on `tty1`
- the existing session supervisor, socket path, waiting states, and display-power behavior must keep working
- Moonlight must get a real Wayland output and desktop-sized window geometry instead of an empty or placeholder output

The project therefore needs a relay-managed `sway` runtime that mirrors the appliance goals of the existing `cage` stack without turning into a general-purpose tiling-desktop deployment.

## Goals / Non-goals

Goals:

- Define one relay-managed `sway` startup path for the kiosk service.
- Generate a minimal `sway` config at runtime instead of relying on operator-owned files.
- Keep the current dedicated-user, seatd, tty1, and systemd ownership model.
- Start the existing `session-entrypoint` inside `sway` without adding a shell wrapper.
- Keep the appliance presentation intentionally constrained and non-desktop-like.

Non-goals:

- Supporting operator-customized `~/.config/sway/config` in the managed kiosk path.
- Introducing a sway bar, launcher, shell, or ordinary desktop workflow.
- Defining support for SPICE, VNC, or Looking Glass on `sway` in this series.
- Adding sway IPC automation or runtime reconfiguration beyond the generated startup config.
- Replacing the existing session supervisor with sway-native window management logic.

## User stories

- As an operator using Moonlight, I want the relay to boot into a supported `sway` session automatically instead of editing the kiosk unit by hand.
- As an operator, I want the generated `sway` environment to stay appliance-like rather than exposing a normal desktop shell.
- As a maintainer, I want one deterministic generated config path that I can inspect when troubleshooting.
- As a troubleshooter, I want journald and runtime files to tell me which generated sway config the kiosk used.

## Public API / Interfaces

This spec reuses the kiosk compositor selection from Spec 50:

```toml
[target]
console_backend = "moonlight"

[kiosk]
compositor = "auto"
```

Managed runtime artifacts:

- generated sway config path: `/run/relayinner-display/sway.config`

Kiosk launcher behavior for `kiosk_compositor = "sway"`:

- generate `/run/relayinner-display/sway.config` before starting the compositor
- ensure the generated file is owned by `relayinner-display`
- exec `sway --config /run/relayinner-display/sway.config`

Generated sway config rules:

- It must not include system or user sway config files.
- It must not define a bar, launcher, terminal, or shell-spawning keybinding.
- It must start the relay session with:

```text
exec /usr/local/lib/relayinner-display/session-entrypoint --config /etc/relayinner-display/config.toml
```

- It may include only the minimum sway directives required to keep the kiosk single-purpose.

Output handling rules:

- If `[display].output_name` is empty, the generated config relies on sway's normal output selection.
- If `[display].output_name` is non-empty, the generated config pins workspace `1` to that output with `workspace 1 output <output_name>`.
- If the named output is unavailable at sway startup, the kiosk logs a warning and continues with sway's ordinary output behavior; this spec does not add a hard startup failure for that case.

Host dependency additions:

- `sway` becomes a required host package when the resolved kiosk compositor is `sway`.
- `seatd`, DRM supplementary groups, and the existing runtime user model remain unchanged.

Service contract:

- `relayinner-display-kiosk.service` remains the owning unit for the kiosk session on `tty1`.
- The service keeps the same `User=relayinner-display`, `Group=relayinner-display`, `TTYPath=/dev/tty1`, and `LIBSEAT_BACKEND=seatd` policy.

## Data model / Persistence

Persistent data remains:

- `/etc/relayinner-display/config.toml`

Runtime-only data added by this spec:

- `/run/relayinner-display/sway.config`

Runtime persistence rules:

- The generated sway config is recreated on each kiosk start.
- The generated sway config is not treated as operator-owned configuration.
- The generated sway config is removed implicitly with the runtime directory on reboot.

## Security model (Permission/Isolation/Audit)

Permissions:

- `sway` runs as the unprivileged `relayinner-display` user.
- The generated sway config is writable only by that runtime user and not world-readable if it contains absolute local paths.

Isolation:

- The managed kiosk path must not load operator sway config from the home directory.
- The generated sway config must not expose shell-launching convenience bindings.
- The session entrypoint remains the only long-lived relay-managed child launched by sway startup.

Audit:

- Log when the kiosk launcher writes `/run/relayinner-display/sway.config`.
- Log the selected output name when `[display].output_name` is set.
- Log a warning when an explicit output pin is requested but sway cannot use it at startup.

## Acceptance criteria (Testable, Verifiable)

- With `kiosk_compositor = "sway"`, the kiosk launcher generates `/run/relayinner-display/sway.config` and starts `sway --config /run/relayinner-display/sway.config`.
- The generated sway config launches `/usr/local/lib/relayinner-display/session-entrypoint --config /etc/relayinner-display/config.toml`.
- The generated sway config does not include user or system sway configs.
- The managed sway path preserves the existing dedicated-user and tty1 ownership model.
- With Moonlight on the managed sway path, the stream starts inside a real output-backed Wayland session instead of the placeholder-output behavior observed on the unsupported Cage path.
- If `[display].output_name` is set, the generated config pins workspace `1` to that output.

## Test plan

Launcher behavior:

- verify sway config generation path
- verify sway exec command shape
- verify generated config ownership and mode

Generated config content:

- includes the session-entrypoint exec line
- does not include a bar definition
- does not include shell-launching bindings
- includes `workspace 1 output <name>` only when `display.output_name` is set

Bootstrap behavior:

- require `sway` when resolved compositor is `sway`
- keep existing `cage` package requirement for the default non-Moonlight path

Manual checks:

- boot with `console_backend = "moonlight"` and default `kiosk.compositor = "auto"`
- verify tty1 enters the managed sway session
- verify Moonlight reaches fullscreen streaming on a real output
- verify waiting and pairing flows still occupy the kiosk session correctly

Regression checks:

- existing `cage` kiosk path remains unchanged for SPICE, VNC, and Looking Glass

## Rollout / Backward compatibility

This spec is additive and runtime-selective:

- existing non-Moonlight installs keep the current `cage` path by default
- Moonlight gains a supported managed sway path through the `auto` compositor resolution
- no operator-owned sway config is introduced or migrated

Operational compatibility rules:

- docs must describe `/run/relayinner-display/sway.config` as generated, runtime-only state
- docs must keep the relay kiosk framed as a dedicated appliance session, not a sway desktop

## Open questions

- Whether a later spec should disable Xwayland explicitly in the generated sway config after wider validation.
- Whether a later spec should add an explicit startup self-check that the requested output pin really mapped to a live output.
- Whether a future series should add a relay-managed way to suppress sway's own default exit keybindings if that becomes necessary in appliance testing.

## Spec Dependencies

- Spec 50. Kiosk Compositor Selection Contract
- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
- Spec 30. Moonlight Backend Contract and Config
- Spec 31. Moonlight Pair-Assist and Persistent Workspace
- Spec 32. Moonlight Stream Launch, Recovery, and Ops
