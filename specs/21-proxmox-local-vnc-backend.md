# Spec 21. Proxmox Local VNC Backend

## Context / Problem

After Spec 20, the appliance can describe multiple console backends, but it still has only one implemented transport. The most pragmatic second backend is VNC because:

- `remote-viewer` already supports VNC
- Proxmox documents external VNC access for VMs
- operators may prefer VNC where SPICE guest tooling is unavailable or undesirable

At the same time, this project should not turn VNC support into a second large control plane with ticket brokers, browser bridges, or cluster-aware proxy management. The backend needs one narrow supported path that fits the existing appliance model and stays supportable on a host-direct install.

This spec therefore defines a local VNC backend around an operator-prepared Proxmox VM that exposes a loopback-only VNC endpoint. The relay consumes that endpoint, supervises the viewer lifecycle, and preserves the current waiting/reconnect/degraded behavior.

## Goals / Non-goals

Goals:

- Add `console_backend = "vnc"` as an implemented runtime path.
- Reuse `remote-viewer` as the fullscreen client for the VNC backend.
- Require only host-local VNC exposure from the VM, not a browser or remote proxy.
- Preserve the existing daemon/session state machine and reconnect behavior.
- Validate that the configured VNC endpoint is loopback-bound and matches relay config.

Non-goals:

- Dynamic Proxmox API ticket handling for noVNC-style console access.
- Opening the VM VNC console on a non-local address.
- Managing or rewriting the VM's `args:` line automatically.
- Multi-user VNC sharing, password rotation, or ACL management.
- Audio, clipboard, or USB policy beyond what the VM already exposes.

## User stories

- As an operator, I can choose VNC in the relay config for a VM that I have already prepared for local-only VNC access.
- As an operator, if the VNC viewer crashes while the VM is still running, the relay automatically reconnects.
- As a maintainer, I can support one narrow VNC path without inheriting browser-console or token-broker complexity.
- As a user at the monitor, I still never see a host login prompt or stale guest image just because the VNC backend is selected.

## Public API / Interfaces

Required VM-side precondition:

- The target VM must expose a local-only VNC endpoint through Proxmox VM configuration.
- The supported configuration shape is equivalent to:

```text
args: -vnc 127.0.0.1:77
```

- The relay supports only loopback binding for this backend.
- The relay does not manage a VNC password in MVP+1. Operators must not bind the VNC server to a non-loopback address in order to compensate.

Relay configuration:

```toml
[target]
vmid = 101
node_name = "auto"
guest_os = "windows"
console_backend = "vnc"

[console]
artifact_dir = "/run/relayinner-display/console"

[console.vnc]
bind_host = "127.0.0.1"
display_number = 77
viewer = "remote-viewer"
```

Derived connection rules:

- TCP port = `5900 + display_number`
- Viewer URI = `vnc://<bind_host>:<port>`
- Supported `bind_host` values are `127.0.0.1` and `localhost`

Daemon responsibilities for VNC:

- Read current VM status through `qm status <vmid>`.
- Read VM configuration through `qm config <vmid>` and verify that the configured VNC exposure matches `[console.vnc]`.
- When the VM is runnable, attempt a short TCP connect to the derived loopback VNC endpoint before telling the session to launch.
- Treat a mismatch between relay config and VM config as a controlled degraded reason.

Daemon-to-session launch contract:

- `{"type":"connect_console","backend":"vnc","launcher":"remote-viewer","argv":["remote-viewer","--full-screen","vnc://127.0.0.1:5977"]}`

Session contract:

- The session launches `remote-viewer` exactly once per active connection attempt.
- Unexpected viewer exit reports `{"type":"console_exited","backend":"vnc","code":...,"signal":...}`.
- Intentional disconnect because the VM stopped suppresses the exit event exactly as the SPICE path already does.

Failure handling:

- VM off -> `show_waiting`
- VM on but VNC endpoint not listening yet -> reconnect path with bounded backoff
- VM config mismatch or non-loopback VNC bind -> `degraded`
- missing `remote-viewer` binary -> `degraded`

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`

Runtime data:

- `/run/relayinner-display/daemon.state.json`

No backend-specific artifact file is required for VNC. The backend launches directly from the derived URI.

Runtime state additions:

```json
{
  "console_backend": "vnc",
  "active_console_backend": "vnc",
  "last_error": null,
  "vnc_endpoint": "127.0.0.1:5977"
}
```

Persistence rules:

- `vnc_endpoint` is runtime-only metadata for troubleshooting.
- No VNC credential is persisted by the relay.
- The daemon revalidates the VM config and endpoint reachability on every fresh start.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon reads VM status and VM config using the same local Proxmox command model as existing specs.
- The session launches `remote-viewer` as the unprivileged relay user.

Isolation:

- The relay supports only loopback-bound VNC for this backend.
- If the VM config binds VNC to `0.0.0.0`, a host LAN address, or any non-loopback interface, the daemon must refuse to connect and enter `degraded`.
- The relay does not expose the VNC endpoint outside the host and does not open firewall rules for it.

Audit:

- Log the validated VNC endpoint at daemon start.
- Log one concise degraded reason for config mismatch, non-loopback bind, or endpoint reachability failure.
- Log viewer launch and exit using `backend=vnc`.

## Acceptance criteria (Testable, Verifiable)

- With a VM configured for loopback VNC and `console_backend = "vnc"`, the relay launches `remote-viewer` fullscreen against the derived `vnc://` URI.
- If `remote-viewer` exits while the VM remains running, the relay re-enters reconnect flow and restores the console automatically.
- If the VM is running but the configured VNC endpoint is not yet reachable, the relay stays on the reconnecting/waiting path instead of exposing stale content.
- If the VM config exposes VNC on a non-loopback address or on a different display number than configured, the relay enters `degraded` with a clear reason.
- If the VM is stopped, the relay behaves exactly like the current SPICE path with waiting and display-sleep logic.

## Test plan

Config validation:

- accept `bind_host = "127.0.0.1"`
- accept `bind_host = "localhost"`
- reject wildcard or non-loopback hosts
- reject negative or out-of-range `display_number`

Daemon behavior:

- VM running and endpoint reachable
- VM running and endpoint not yet reachable
- VM stopped
- `qm config` mismatch against configured display number
- missing `remote-viewer`

Session behavior:

- verify `connect_console` launches `remote-viewer --full-screen vnc://...`
- verify unexpected exit produces backend-tagged `console_exited`
- verify intentional disconnect suppresses the exit event

Manual checks:

- confirm only one viewer process exists at a time
- confirm a misconfigured non-loopback VM VNC bind moves into `degraded`

## Rollout / Backward compatibility

This backend is additive:

- existing SPICE installs remain unchanged
- VNC is activated only when `console_backend = "vnc"`

Operator migration rules:

- The README and host setup guide must document the exact Proxmox VM-side precondition for loopback VNC.
- The relay installer must not silently rewrite VM configuration to enable VNC.
- If operators switch from SPICE to VNC without preparing the VM config, the failure mode must be explicit and controlled.

## Open questions

- Whether `remote-viewer` URI launch is sufficient on every supported host package version or if a generated `.vv` file should be added later for parity with SPICE.
- Whether future hardening should manage an optional VNC password even for loopback-only access.
- Whether some Proxmox releases normalize `qm config` output for `args:` in a way that requires more tolerant matching logic.

## Spec Dependencies

- Spec 20. Configurable Console Backend Contract
