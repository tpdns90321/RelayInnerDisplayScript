# Spec 85. Host VM-Control Proxy

## Context / Problem

The current daemon controls Proxmox through local host commands such as `qm` and `pvesh`. In the LXC direct-seat profile, the daemon runs inside an unprivileged container and must not receive broad host command execution access. A small host-side proxy is required to preserve the relay's local control semantics while exposing only the operations needed for one configured VM.

## Goals / Non-goals

Goals:

- Add a host-side VM-control proxy for the LXC direct-seat profile.
- Expose a narrow domain-specific RPC API over a Unix socket.
- Restrict all operations to one configured VMID.
- Keep authentication limited to Unix socket filesystem permissions for the MVP.
- Preserve the existing host-direct local command path.
- Provide enough operations for SPICE and VNC MVP backends.

Non-goals:

- Exposing arbitrary `qm` or `pvesh` subcommands.
- Supporting multiple VM switching.
- Adding TCP listeners or remote network access.
- Storing Proxmox API tokens in the LXC.
- Replacing host-direct operation.

## User stories

- As an operator, I want the LXC daemon to control the target VM without giving the container shell access to Proxmox host commands.
- As a maintainer, I want the proxy interface to match relay domain operations instead of raw command execution.
- As a reviewer, I want the proxy to reject every VMID except the configured one.

## Public API / Interfaces

Host proxy systemd unit:

```text
relayinner-display-host-proxy.service
```

Unix socket path, subject to final install profile rendering:

```text
/run/relayinner-display-host-proxy/proxy.sock
```

Allowed RPC methods:

```text
get_vm_status
start_vm
shutdown_vm
create_spice_ticket
get_vnc_endpoint
probe_vnc_endpoint
```

The wire format should be deterministic JSON over a Unix stream socket unless implementation proves an existing internal framing protocol is better to reuse. Example request:

```json
{"method":"get_vm_status","vmid":100,"id":"request-1"}
```

Example response:

```json
{"id":"request-1","ok":true,"result":{"status":"running"}}
```

The proxy must reject:

- unknown methods
- missing request IDs
- VMID values other than the configured target
- shell fragments or raw command requests
- requests with unexpected fields when strict validation is possible

## Data model / Persistence

Host proxy config stores:

- target VMID
- Proxmox node name if required by existing command wrappers
- socket path
- socket group
- command timeout

Install-state stores:

- proxy unit path
- socket directory path
- socket group ownership policy
- whether the proxy group was created by installer

Runtime state may expose proxy availability and last proxy error in daemon diagnostics, but must not log SPICE ticket secrets at info level.

## Security model (Permission/Isolation/Audit)

Permissions:

- The proxy runs on the Proxmox host with the minimum privileges needed to call `qm` and `pvesh` for the target VM.
- The LXC accesses the proxy only through a bind-mounted Unix socket.
- MVP authorization is Unix socket group permission.

Isolation:

- No TCP socket is opened.
- No arbitrary command execution exists in the proxy protocol.
- The proxy validates VMID server-side even if the client is compromised.

Audit:

- Each accepted method call is logged with method name, VMID, result category, and request ID.
- Secret-bearing fields, including SPICE ticket material, are redacted from logs.
- Rejected method calls are logged with reason and peer credential information when available.

## Acceptance criteria (Testable, Verifiable)

- The host proxy starts as a systemd service in the LXC host profile.
- The proxy creates a Unix socket with the configured group permission.
- The proxy accepts only the six allowed high-level methods.
- The proxy rejects all VMIDs except the configured target VMID.
- The proxy never accepts raw `qm`, `pvesh`, or shell command payloads.
- `get_vm_status`, `start_vm`, and `shutdown_vm` map to existing local Proxmox behavior.
- `create_spice_ticket` returns enough data for the LXC daemon to create a local `.vv` file without exposing host paths.
- VNC endpoint methods support the later LXC VNC forwarding contract.
- Host-direct mode continues using local commands without the proxy.

## Test plan

Automated tests should cover:

- request/response framing
- method allowlist validation
- VMID mismatch rejection
- malformed JSON rejection
- secret redaction
- command wrapper invocation for each allowed method
- timeout handling
- Unix socket permission rendering

Integration validation on Proxmox:

1. Install host proxy for one target VMID.
2. Bind the socket into the LXC.
3. From the LXC, call `get_vm_status` through the proxy client.
4. Start and shut down a test VM through the proxy.
5. Verify host logs contain audited method names but no raw secrets.

## Rollout / Backward compatibility

The proxy is installed only for the LXC direct-seat host profile. Host-direct installs do not require or start it.

The proxy API must be versioned or self-describing before future multi-method expansion. Backward-incompatible wire changes must be gated by profile or protocol version.

## Open questions

- Should the proxy use a new standalone module or share the existing daemon IPC framing helpers?
- Should peer credentials from `SO_PEERCRED` be required in addition to socket group permissions?
- What exact VNC forwarding lifecycle belongs in this proxy versus a separate host service?

## Spec Dependencies

- Spec 10: Proxmox Local Console Relay Core
- Spec 20: Configurable Console Backend Contract
- Spec 21: Proxmox Local VNC Backend
- Spec 82: LXC Host Bootstrap Profile
- Spec 83: LXC Guest Install Profile
