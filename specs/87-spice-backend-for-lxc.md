# Spec 87. SPICE Backend for LXC

## Context / Problem

SPICE is the default console backend in the host-direct relay. In LXC direct-seat mode, the daemon and session run inside the container, but Proxmox SPICE ticket creation must remain on the host side through the VM-control proxy. The container should create and launch its own local `.vv` artifact rather than receiving a host path that may not exist or should not be exposed.

## Goals / Non-goals

Goals:

- Support the SPICE backend in LXC direct-seat mode.
- Obtain SPICE ticket data through the host proxy.
- Generate the `.vv` file inside the container runtime directory.
- Reuse existing certificate and multiline escaping behavior.
- Launch `remote-viewer` inside the LXC kiosk session.
- Preserve host-direct SPICE behavior.

Non-goals:

- Calling `pvesh` directly from the LXC.
- Bind-mounting host-generated `.vv` files into the container.
- Adding browser/noVNC support.
- Supporting multiple target VMs.
- Changing the SPICE viewer allowlist.

## User stories

- As an operator, I want the default SPICE console path to work when the relay runtime runs inside LXC.
- As a maintainer, I want SPICE ticket creation to remain on the host without exposing host command execution to the container.
- As a reviewer, I want `.vv` files and ticket secrets to stay in the container runtime lifecycle and be redacted from logs.

## Public API / Interfaces

Host proxy method:

```text
create_spice_ticket
```

Expected proxy result shape, conceptually:

```json
{
  "type": "spice_ticket",
  "fields": {
    "host": "127.0.0.1",
    "proxy": "...",
    "tls-port": "...",
    "password": "...",
    "ca": "..."
  }
}
```

The exact field names should match the existing local SPICE generation model where practical.

Container-local artifact path:

```text
/run/relayinner-display/console/spice-current.vv
```

The daemon sends the existing backend-neutral session IPC to launch `remote-viewer` against the container-local file.

## Data model / Persistence

Runtime artifacts:

- Container-local `.vv` file under `/run/relayinner-display/console/`.
- Existing daemon runtime state may continue to expose active backend and console status.

No SPICE ticket data is persisted across daemon restarts. The `.vv` file is runtime-only and should be overwritten on each new ticket.

Runtime diagnostics must not expose ticket password values.

## Security model (Permission/Isolation/Audit)

Permissions:

- Only the host proxy can call Proxmox ticket commands.
- The LXC daemon can request ticket data only through the proxy socket.
- The `.vv` file is written with permissions readable only by the relay runtime user/group as needed by the session process.

Isolation:

- No host path to a `.vv` file is shared into the container.
- The proxy is still restricted to one configured VMID.
- Host-direct local SPICE generation remains separate from LXC proxy SPICE generation.

Audit:

- Logs may state that a SPICE ticket was requested and a `.vv` file was written.
- Logs must redact password and certificate material unless debug tooling explicitly opts into safe test fixtures.
- Proxy request IDs should be correlated with daemon logs without leaking secrets.

## Acceptance criteria (Testable, Verifiable)

- In LXC profile, SPICE preparation calls `create_spice_ticket` on the host proxy instead of local `pvesh`.
- The daemon writes a container-local `.vv` file under the runtime console directory.
- Multiline certificate values are escaped in the `.vv` file exactly as required by `remote-viewer`.
- The session launches `remote-viewer` with the container-local `.vv` path.
- SPICE ticket secrets are not logged at info level.
- Host-direct SPICE tests and behavior remain unchanged.
- Proxy failures degrade through the existing console preparation failure path.

## Test plan

Automated tests should cover:

- proxy SPICE response mapping
- `.vv` file generation from proxy fields
- multiline certificate escaping
- file permissions for generated artifacts
- session IPC path in LXC profile
- proxy timeout and error handling
- host-direct regression tests

Manual validation:

1. Start a target VM with SPICE available.
2. Run the relay in LXC direct-seat profile.
3. Confirm daemon requests a SPICE ticket through the host proxy.
4. Confirm `.vv` exists inside the container runtime directory.
5. Confirm `remote-viewer` displays the guest fullscreen on the attached monitor.

## Rollout / Backward compatibility

SPICE remains the default backend for host-direct installs. LXC SPICE support is enabled only when both `runtime.profile = "lxc-direct-seat"` and `console_backend = "spice"` are configured.

Existing host-direct `.vv` generation behavior must not change except for shared helper refactoring covered by tests.

## Open questions

- Should the proxy return raw `spiceproxy` fields or a ready-to-render `.vv` data structure?
- Should the container daemon or host proxy own validation of missing SPICE fields?
- Should LXC mode support hostnames other than loopback in generated SPICE data?

## Spec Dependencies

- Spec 10: Proxmox Local Console Relay Core
- Spec 20: Configurable Console Backend Contract
- Spec 85: Host VM-Control Proxy
- Spec 86: Proxmox Client Interface Split
