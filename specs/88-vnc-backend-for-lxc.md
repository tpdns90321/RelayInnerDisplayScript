# Spec 88. VNC Backend for LXC

## Context / Problem

The current VNC backend assumes a Proxmox host loopback endpoint such as `127.0.0.1:<display>`. Inside an LXC, `127.0.0.1` refers to the container, not the Proxmox host. LXC direct-seat mode therefore needs a safe host-mediated endpoint or forward while preserving the existing loopback-only security policy for the VM's QEMU VNC exposure.

## Goals / Non-goals

Goals:

- Support the existing loopback-only VNC backend from LXC direct-seat mode.
- Keep the VM's QEMU VNC bind restricted to host loopback.
- Provide an LXC-reachable endpoint through the host proxy or a host-managed forward.
- Preserve existing VNC config validation semantics.
- Expose runtime diagnostics for the forwarded endpoint.

Non-goals:

- Allowing QEMU VNC to bind to a LAN address.
- Sharing the host network namespace with the container.
- Adding authentication to the QEMU VNC server beyond the existing backend contract.
- Supporting arbitrary TCP forwarding.
- Supporting non-loopback VM VNC exposure.

## User stories

- As an operator, I want the VNC backend to work in LXC mode without weakening the QEMU VNC bind policy.
- As a maintainer, I want forwarding lifecycle and diagnostics to be explicit rather than hidden in shell scripts.
- As a reviewer, I want the LXC to receive only a relay-managed endpoint for the configured VM.

## Public API / Interfaces

Host proxy methods:

```text
get_vnc_endpoint
probe_vnc_endpoint
```

For LXC mode, `get_vnc_endpoint` must return a container-reachable endpoint, not raw host `127.0.0.1`, unless a forward has made that address valid inside the container.

Conceptual response:

```json
{
  "host_endpoint": "127.0.0.1:5901",
  "container_endpoint": "<lxc-reachable-host>:<forward-port>",
  "forward_managed": true
}
```

The daemon passes the container endpoint to the session launch contract for `remote-viewer`.

Forwarding implementation is intentionally left to implementation choice, but it must be host-managed and tied to the configured single VM/backend. Possible approaches include a host-local TCP forward bound only to the container bridge or another narrow host-to-container path.

## Data model / Persistence

Runtime state should expose:

```json
{
  "vnc_endpoint": "<container endpoint>",
  "vnc_host_endpoint": "127.0.0.1:<port>",
  "vnc_forward_managed": true
}
```

Install-state should record any persistent forward service or socket unit installed by the host profile.

Ephemeral forward process state should be restart-safe. If the proxy or forward restarts, the daemon must re-probe before launching `remote-viewer`.

## Security model (Permission/Isolation/Audit)

Permissions:

- Only the host proxy manages or reports VNC forwarding.
- The LXC daemon cannot request arbitrary host ports.
- The VMID and configured display number remain server-side validated.

Isolation:

- QEMU VNC remains bound to host loopback.
- The forward binds as narrowly as possible for the target LXC.
- No host network namespace sharing is required.

Audit:

- Logs must show host endpoint, container endpoint, and forward lifecycle state.
- Logs must identify when an endpoint is refused because the QEMU config is non-loopback.
- Forward setup and teardown must be visible in host proxy or systemd logs.

## Acceptance criteria (Testable, Verifiable)

- Existing host-direct VNC loopback validation remains unchanged.
- In LXC profile, raw host `127.0.0.1:<port>` is not passed to the container session unless explicitly made reachable by the managed forward design.
- The host proxy validates that QEMU VNC is still loopback-only.
- The host proxy returns a container-reachable endpoint for the configured VM only.
- The daemon waits for `probe_vnc_endpoint` success before launching `remote-viewer`.
- Runtime state records the LXC-facing endpoint and host endpoint separately.
- Non-loopback QEMU VNC config is refused.
- Forward teardown occurs on uninstall or profile disable.

## Test plan

Automated tests should cover:

- host loopback config validation
- LXC endpoint mapping from host proxy response
- refusal of non-loopback binds
- probe success and timeout behavior
- runtime state fields
- daemon launch payload for `remote-viewer`
- uninstall of any managed forward units

Manual validation:

1. Configure a VM with loopback-only VNC on the Proxmox host.
2. Start the LXC direct-seat host proxy/forward path.
3. Confirm the LXC cannot depend on host `127.0.0.1` directly.
4. Confirm the proxy returns a reachable forwarded endpoint.
5. Confirm `remote-viewer` displays the VM through VNC from inside the kiosk session.
6. Confirm no LAN-facing QEMU VNC listener was created.

## Rollout / Backward compatibility

VNC remains supported in host-direct mode exactly as before. LXC VNC support is enabled only for `runtime.profile = "lxc-direct-seat"` with `console_backend = "vnc"`.

If the forward mechanism is unavailable, LXC VNC must degrade with a clear reason rather than falling back to non-loopback VM exposure.

## Open questions

- Should forwarding be owned by the host proxy process, a separate systemd socket/service, or a short-lived helper managed by the proxy?
- What is the safest bind address for a Proxmox bridge with one target CT?
- Should the host profile allocate a fixed forward port or choose an ephemeral port and report it through the proxy?

## Spec Dependencies

- Spec 20: Configurable Console Backend Contract
- Spec 21: Proxmox Local VNC Backend
- Spec 85: Host VM-Control Proxy
- Spec 86: Proxmox Client Interface Split
