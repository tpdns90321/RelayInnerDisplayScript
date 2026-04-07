# Spec 41. Moonlight Client Resolution Override

## Context / Problem

Specs 30, 32, and 40 establish the current Moonlight backend contract:

- operators select Moonlight through `console_backend = "moonlight"`
- the relay prepares and preserves a managed Moonlight workspace
- the daemon validates pairing and app availability, then launches `moonlight stream`
- the relay treats paired `Desktop` launches differently from non-`Desktop` app validation

That contract still leaves one operational gap: the relay does not let operators declare the Moonlight client resolution in `config.toml`.

Today, resolution behavior depends on Moonlight's own persisted workspace settings or prior manual interaction outside the relay config surface. That is weak for an appliance-style deployment because:

- the effective stream resolution is not visible in the relay's source-of-truth config
- replacing or reusing the managed Moonlight workspace can silently change behavior
- operators cannot intentionally pin a lower or non-default stream size for bandwidth, decode-performance, or panel-compatibility reasons
- troubleshooting a blank, blurry, scaled, or overloaded stream requires checking Moonlight-internal state rather than the relay contract

The relay needs one small, explicit Moonlight tuning surface that keeps the appliance model intact without opening the full Moonlight preference matrix.

## Goals / Non-goals

Goals:

- Add a config-driven Moonlight client resolution override.
- Keep the override visible and auditable in `/etc/relayinner-display/config.toml`.
- Apply the requested resolution on every relay-managed Moonlight launch.
- Validate the value early so malformed resolution strings fail before runtime.
- Expose the configured resolution in runtime diagnostics.

Non-goals:

- Adding FPS, bitrate, HDR, codec, or other Moonlight tuning knobs in this spec.
- Editing Moonlight's QSettings or `.ini` files directly inside `state_dir`.
- Detecting a "native" resolution automatically from EDID or Wayland output metadata.
- Negotiating or forcing guest-side desktop resolution through Sunshine.
- Adding per-app resolution profiles or dynamic resolution switching.

## User stories

- As an operator using a 4K TV, I want to pin the Moonlight stream to `1920x1080` from config so the appliance stays stable and bandwidth stays predictable.
- As an operator using a non-standard monitor, I want to request a custom stream size such as `3440x1440` without editing Moonlight's UI settings manually.
- As a troubleshooter, I want the runtime state file to show the requested Moonlight resolution so I can correlate config with actual launch behavior quickly.
- As an existing Moonlight user, I want leaving the new setting unset to preserve the current behavior.

## Public API / Interfaces

Primary config file remains:

- Path: `/etc/relayinner-display/config.toml`
- Format: TOML

This spec adds one optional Moonlight config key:

```toml
[target]
console_backend = "moonlight"

[console.moonlight]
binary = "moonlight"
host = "192.168.50.20"
base_port = 47989
app = "Desktop"
resolution = "1920x1080"
state_dir = "/var/lib/relayinner-display/moonlight"
quit_app_after_session = false
```

Configuration rules:

- `resolution` is optional.
- If `resolution` is omitted, the relay does not pass a resolution override to Moonlight and current behavior remains unchanged.
- If present, `resolution` must be a string in `<width>x<height>` form.
- `x` may be lowercase or uppercase in config input, but the relay normalizes it to lowercase `x`.
- `width` and `height` must both parse as positive base-10 integers greater than zero.
- Surrounding whitespace is ignored during parsing; the stored canonical value is `<width>x<height>` with no surrounding spaces.
- Values such as `1080p`, `1920*1080`, `0x1080`, `1920x0`, or `1920 x 1080` are invalid.

Launch contract changes:

- If `resolution` is configured, the daemon emits:

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
    "--resolution",
    "1920x1080",
    "--display-mode",
    "fullscreen"
  ]
}
```

- The exact Moonlight argv order for this spec is:
  - `moonlight`
  - `stream`
  - `<host-authority>`
  - `<app>`
  - optional `--resolution <width>x<height>`
  - `--display-mode fullscreen`
  - optional `--quit-after`
- If `resolution` is not configured, the relay keeps the existing argv shape from Spec 32.

Runtime state additions:

```json
{
  "console_backend": "moonlight",
  "moonlight_app": "Desktop",
  "moonlight_resolution": "1920x1080"
}
```

Runtime-state rules:

- `moonlight_resolution` is `null` when no override is configured.
- `moonlight_resolution` reflects the configured requested value, not a negotiated or measured stream size.

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`
- managed Moonlight `state_dir`

Runtime data:

- `/run/relayinner-display/daemon.state.json`
- `/run/relayinner-display/session.sock`

Persistence rules:

- The relay persists the requested Moonlight resolution only in `config.toml`.
- The relay mirrors the configured resolution into runtime state for diagnostics.
- The relay does not rewrite Moonlight settings files under `state_dir` to store width or height.
- The relay continues to treat Moonlight's managed workspace as Moonlight-owned file content.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon remains responsible for config validation and launch-message construction.
- The session still launches Moonlight as the unprivileged relay session user.

Isolation:

- `resolution` is validated as structured data, not interpolated into a shell command.
- The session continues to spawn Moonlight directly without a shell wrapper.
- This spec does not expand the backend into arbitrary Moonlight CLI passthrough.

Audit:

- Log the configured Moonlight resolution at launch preparation when an override is present.
- Include `moonlight_resolution` in runtime diagnostics.
- Do not log or modify Moonlight's internal settings blobs.

## Acceptance criteria (Testable, Verifiable)

- Config parsing accepts a valid Moonlight `resolution` override such as `1920x1080`.
- Config parsing rejects malformed Moonlight resolution values before runtime.
- When `resolution` is configured, the daemon includes `--resolution <width>x<height>` in the Moonlight launch argv.
- When `resolution` is omitted, the daemon keeps the current Moonlight launch argv unchanged.
- `moonlight_resolution` appears in runtime state when configured and remains `null` otherwise.
- Existing Moonlight pairing, Desktop fast-path behavior, non-`Desktop` app validation, and reconnect behavior remain unchanged.

## Test plan

Config validation:

- accept `resolution = "1920x1080"`
- accept `resolution = "3440X1440"` and normalize to `3440x1440`
- reject malformed strings such as `1080p`, `1920*1080`, `1920 x 1080`, `0x1080`, and `1920x0`

Daemon behavior:

- Moonlight launch with configured resolution includes `--resolution` before `--display-mode`
- Moonlight launch without configured resolution keeps the pre-Spec-41 argv
- runtime state includes `moonlight_resolution` when configured
- runtime state leaves `moonlight_resolution` unset when omitted

Session and IPC regression:

- `connect_console` with the new Moonlight argv shape still passes existing validation
- Moonlight launcher allowlist and `cwd` rules remain unchanged

Documentation checks:

- sample config comments describe the new Moonlight `resolution` key
- README and setup docs describe the override as optional and config-driven
- spec indexes include Spec 41 and place it after Spec 40 in dependency order

Manual checks:

- paired `Desktop` launch with `resolution = "1920x1080"`
- paired non-`Desktop` launch with a custom resolution override
- unchanged launch behavior when the key is removed from config

## Rollout / Backward compatibility

This spec is additive and backward-compatible:

- existing Moonlight configs remain valid without modification
- no migration is required for `state_dir`
- existing relay-managed Moonlight workspaces remain usable

Precedence rules:

- If `resolution` is configured, the launch-time CLI override takes precedence over whatever width or height Moonlight may already remember in its managed workspace.
- If `resolution` is not configured, Moonlight continues to use its ordinary behavior from the managed workspace and defaults.

Documentation rules:

- README, setup docs, and sample config must present `resolution` as an optional Moonlight-only override.
- Documentation must not imply that the relay edits Sunshine or guest display settings directly.

## Open questions

- Whether a later spec should add a curated Moonlight performance bundle for `resolution`, `fps`, and `bitrate` together.
- Whether a future spec should support symbolic values such as `native` or `monitor-preferred` instead of explicit dimensions only.
- Whether runtime diagnostics should eventually expose both the configured requested resolution and the last observed negotiated stream mode.

## Spec Dependencies

- Spec 30. Moonlight Backend Contract and Config
- Spec 32. Moonlight Stream Launch, Recovery, and Ops
- Spec 40. Moonlight Desktop Fast-Path and Pair-State Resilience
- Spec 15. MVP Integration, Failure Policy, and Ops
