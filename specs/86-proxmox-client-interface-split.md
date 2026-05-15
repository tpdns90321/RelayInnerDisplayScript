# Spec 86. Proxmox Client Interface Split

## Context / Problem

The daemon currently assumes a local Proxmox command client. LXC direct-seat mode needs the same daemon state machine to talk to a host proxy instead, without scattering profile-specific conditionals throughout the daemon. The Proxmox control boundary should become an interface with local and proxy-backed implementations.

## Goals / Non-goals

Goals:

- Preserve the existing local command client for host-direct installs.
- Add a proxy-backed client that implements the same daemon-facing operations.
- Select the implementation from runtime profile config.
- Keep daemon state-machine logic backend-neutral.
- Ensure tests can run both implementations through shared behavior cases.

Non-goals:

- Rewriting the daemon state machine.
- Removing local `qm` and `pvesh` support.
- Adding Proxmox API token support.
- Supporting multiple target VMs.
- Implementing the host proxy service itself; that belongs to Spec 85.

## User stories

- As a maintainer, I want one daemon code path for VM lifecycle decisions regardless of host-direct or LXC deployment.
- As an operator, I want host-direct installs to continue working exactly as before.
- As a reviewer, I want LXC mode to use a proxy seam rather than direct host command bind mounts.

## Public API / Interfaces

Runtime profile config:

```toml
[runtime]
profile = "host-direct" # or "lxc-direct-seat"

[lxc]
host_proxy_socket = "/run/relayinner-display-host-proxy/proxy.sock"
```

Daemon-facing Proxmox client operations should remain aligned with existing behavior, including:

```text
get_vm_status
start_vm
shutdown_vm
create_spice_ticket / prepare_spice_viewer_file inputs
get_vnc_endpoint
probe_vnc_endpoint
```

The local implementation keeps using host commands. The proxy implementation serializes calls to the host proxy API from Spec 85 and returns the same domain objects or exceptions expected by the daemon.

Profile selection rules:

- `host-direct` selects the local client.
- `lxc-direct-seat` selects the proxy client.
- Unknown profiles fail config validation.
- Missing proxy socket path in `lxc-direct-seat` fails config validation.

## Data model / Persistence

Config model additions:

```toml
[runtime]
profile = "host-direct"

[lxc]
host_proxy_socket = "/run/relayinner-display-host-proxy/proxy.sock"
```

The config parser must preserve backward compatibility by defaulting absent `[runtime].profile` to `host-direct`.

Runtime diagnostics may include:

```json
{
  "runtime_profile": "lxc-direct-seat",
  "proxmox_control_path": "host-proxy",
  "host_proxy_socket": "/run/relayinner-display-host-proxy/proxy.sock"
}
```

## Security model (Permission/Isolation/Audit)

Permissions:

- The proxy client opens only the configured Unix socket.
- The proxy client does not execute shell commands.
- The local client remains available only in host-direct profile.

Isolation:

- Daemon code should not branch directly on LXC mode except at dependency construction and diagnostics.
- Secrets returned by proxy methods must be handled with the same redaction rules as local command results.

Audit:

- Config validation errors must clearly state when an LXC profile is missing proxy settings.
- Daemon startup logs must identify the selected control path.

## Acceptance criteria (Testable, Verifiable)

- Existing config files without `[runtime]` continue to parse as `host-direct`.
- `runtime.profile = "host-direct"` selects the local Proxmox client.
- `runtime.profile = "lxc-direct-seat"` selects the proxy Proxmox client.
- Unknown profile values fail validation.
- LXC profile without a proxy socket path fails validation.
- The daemon can run its existing VM state transitions against a fake local client and a fake proxy client without profile-specific branches in transition logic.
- The proxy client maps proxy errors to existing daemon degradation paths.
- Host-direct tests continue to pass without a host proxy.

## Test plan

Automated tests should cover:

- config parsing defaults
- profile enum validation
- proxy socket path validation
- client factory selection
- proxy client request mapping
- proxy error mapping
- daemon transition tests with both client implementations
- runtime state diagnostics

Manual validation:

1. Start host-direct config and confirm local command path logs.
2. Start LXC profile config with missing socket and confirm validation failure.
3. Start LXC profile config with fake or real proxy socket and confirm proxy path logs.

## Rollout / Backward compatibility

The default profile is `host-direct`, so existing operators do not need to edit config.

The new `[runtime]` and `[lxc]` sections are additive. Documentation must clearly state that CTID is not a runtime config field and belongs to host install commands or install-state.

## Open questions

- Should `[lxc]` be rejected entirely in host-direct profile or allowed but ignored with a warning?
- Should the proxy socket path default be generated by install profile or hard-coded in config defaults?
- Should future Proxmox API-token support become a third client implementation under the same interface?

## Spec Dependencies

- Spec 10: Proxmox Local Console Relay Core
- Spec 20: Configurable Console Backend Contract
- Spec 21: Proxmox Local VNC Backend
- Spec 85: Host VM-Control Proxy
