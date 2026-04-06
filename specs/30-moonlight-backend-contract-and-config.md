# Spec 30. Moonlight Backend Contract and Config

## Context / Problem

Specs 20 through 22 generalized the relay around backend-neutral console launch, then added:

- Proxmox SPICE through `remote-viewer`
- loopback-only Proxmox VNC through `remote-viewer`
- preflight-only Looking Glass through `looking-glass-client`

Those backends all assume the Proxmox host either brokers the console directly or consumes a host-visible shared-memory device. A Moonlight client path changes that shape:

- the displayed session comes from a Sunshine-compatible stream inside the guest
- the relay launches a network streaming client rather than a Proxmox console viewer
- the client needs its own persistent identity and paired-host workspace
- the appliance still needs the same kiosk, reconnect, waiting, and degraded behavior as the existing backends

The project therefore needs a new backend contract for Moonlight that fits the current daemon/session model without reopening the entire backend-neutral architecture again.

## Goals / Non-goals

Goals:

- Add `console_backend = "moonlight"` as a first-class backend option.
- Support Sunshine-backed guests through the Linux `moonlight-qt` CLI entrypoint.
- Keep the current appliance UX: one fullscreen child, one monitor, one VM, one session supervisor.
- Use explicit host addressing from config instead of auto-discovery.
- Define the persistent Moonlight workspace location and the launch contract needed to use it.

Non-goals:

- Supporting GeForce Experience or other legacy NVIDIA-only host software in this series.
- Adding mDNS host discovery, no-touch guest-IP derivation, or a host picker UI.
- Exposing the full Moonlight preference matrix in relay config.
- Supporting Flatpak- or Snap-specific wrapper launch flows as first-class runtime paths.
- Replacing SPICE as the default backend.

## User stories

- As an operator, I can switch the relay to a Moonlight client path when the guest already runs Sunshine.
- As an operator, I can point the relay at a known guest-side Sunshine address instead of relying on discovery.
- As a maintainer, I can add Moonlight without changing the existing daemon/session split or the single-child kiosk model.
- As a user at the monitor, I still get the same appliance-like fullscreen relay behavior rather than an ordinary desktop session.

## Public API / Interfaces

Primary configuration file remains:

- Path: `/etc/relayinner-display/config.toml`
- Format: TOML

Backend selection expands to:

- `spice`
- `vnc`
- `looking-glass`
- `moonlight`

Moonlight configuration:

```toml
[target]
vmid = 101
node_name = "auto"
guest_os = "windows"
console_backend = "moonlight"

[console]
artifact_dir = "/run/relayinner-display/console"

[console.moonlight]
binary = "moonlight"
host = "192.168.50.20"
base_port = 47989
app = "Desktop"
state_dir = "/var/lib/relayinner-display/moonlight"
quit_app_after_session = false
```

Configuration rules:

- `binary` defaults to `moonlight`.
- `binary` may be either a bare executable name or an absolute path.
- `host` is required and must be a non-empty hostname, IPv4 literal, or IPv6 literal without a URL scheme.
- `base_port` defaults to `47989` and represents the Sunshine/GameStream base HTTP port.
- `app` defaults to `Desktop`.
- `state_dir` defaults to `/var/lib/relayinner-display/moonlight` and must be an absolute path.
- `quit_app_after_session` defaults to `false`.
- `console_backend = "moonlight"` requires `moonlight-qt` version `6.0.0` or newer so CLI stream launch has the modern wake-and-stream behavior introduced after the earlier pair/list-only CLI surface.

Rendered host authority rules:

- If `base_port = 47989`, the relay passes `host` directly to Moonlight.
- If `base_port != 47989`, the relay renders `<host>:<base_port>` for hostnames and IPv4, and `[<host>]:<base_port>` for IPv6.

Daemon-to-session IPC extends `connect_console` with an optional working-directory field:

- `{"type":"connect_console","backend":"moonlight","launcher":"moonlight","cwd":"/var/lib/relayinner-display/moonlight","argv":["moonlight","stream","192.168.50.20","Desktop","--display-mode","fullscreen"]}`

`connect_console` contract additions:

- `cwd` is optional for existing backends.
- If `cwd` is present, it must be an absolute path prepared by the daemon.
- The session must apply `cwd` before spawning the viewer child.

Session launch rules:

- `moonlight` is launched as one fullscreen child like the existing backends.
- The session must reject any `backend = "moonlight"` launch where `argv[0]` does not exactly match the configured Moonlight binary for that backend.
- The session must not insert a shell wrapper around Moonlight launch.

Daemon scope in this spec:

- validate the selected Moonlight binary exists
- validate the configured binary version is at least `6.0.0`
- validate `host`, `base_port`, and `state_dir`
- prepare `state_dir` and pass it through `cwd`

This spec intentionally does not yet define the pair-assist state machine or app-list validation details. Those are introduced in Specs 31 and 32.

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`
- `/var/lib/relayinner-display/moonlight/` or the configured `state_dir`

Persistent workspace rules:

- `state_dir` is relay-managed but Moonlight-owned at the file-content level.
- The relay creates `state_dir` and a `portable.dat` marker so Moonlight stores its QSettings and cache data inside the managed workspace.
- The relay does not rewrite Moonlight's internal QSettings structure directly.

Runtime data:

- `/run/relayinner-display/daemon.state.json`
- `/run/relayinner-display/session.sock`

Runtime state in this spec continues to rely on the backend-neutral fields from Spec 20:

```json
{
  "console_backend": "moonlight",
  "active_console_backend": null,
  "vm_power_state": "running",
  "session_state": "requesting_console",
  "last_error": null
}
```

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon remains responsible for validating the configured binary and preparing `state_dir`.
- The Moonlight process itself runs as the unprivileged relay session user.
- The managed Moonlight workspace must not be world-readable because it contains client identity material and host metadata created by Moonlight.

Isolation:

- The backend remains a curated launcher path, not a general arbitrary-command feature.
- The session may launch only the configured Moonlight binary for `backend = "moonlight"`.
- The relay does not store Sunshine web-UI credentials in this spec.

Audit:

- Log backend selection as `backend=moonlight`.
- Log the configured Moonlight host authority and workspace path at daemon startup.
- Do not log Moonlight certificates, QSettings blobs, or future pairing secrets.

## Acceptance criteria (Testable, Verifiable)

- Config parsing accepts `console_backend = "moonlight"` and rejects malformed Moonlight config.
- `connect_console` accepts a Moonlight launch with `cwd` and rejects invalid working-directory values.
- The session can spawn Moonlight from the provided working directory without using a shell wrapper.
- The daemon rejects a missing Moonlight binary or a Moonlight version older than `6.0.0`.
- Existing SPICE, VNC, and Looking Glass config and IPC behavior remain unchanged.

## Test plan

Config validation:

- accept valid Moonlight config with bare `binary`
- accept valid Moonlight config with absolute-path `binary`
- reject empty `host`
- reject relative `state_dir`
- reject out-of-range `base_port`

IPC validation:

- accept `connect_console` for `backend = "moonlight"` with absolute `cwd`
- reject relative `cwd`
- reject `launcher` mismatches for the Moonlight backend

Session behavior:

- verify launch uses the provided `cwd`
- verify the session rejects a Moonlight launch whose `argv[0]` differs from the configured binary

Daemon behavior:

- missing binary
- old binary version
- valid binary version

Regression checks:

- existing SPICE, VNC, and Looking Glass IPC fixtures remain green

## Rollout / Backward compatibility

This spec is additive and opt-in:

- default backend remains `spice`
- service names remain unchanged
- current Proxmox-backed backends remain supported exactly as before

Documentation rules:

- The sample config and README must describe Moonlight as a new planned backend path for Sunshine-backed guests.
- The docs must say explicitly that this series targets `moonlight-qt` on Linux, not Moonlight mobile clients or browser flows.
- The docs must say explicitly that Sunshine setup inside the guest remains operator-managed.

## Open questions

- Whether a later spec should expose a curated Moonlight tuning subset such as bitrate or HDR in relay config.
- Whether future packaging work should support wrapper-based launches such as `flatpak run` instead of direct executables only.
- Whether a later backend should derive the Sunshine host address from guest-agent data rather than config.

## Spec Dependencies

- Spec 20. Configurable Console Backend Contract
- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
