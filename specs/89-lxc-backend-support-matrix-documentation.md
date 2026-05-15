# Spec 89. LXC Backend Support Matrix Documentation

## Context / Problem

The repository currently documents a host-direct appliance model and several console backends. LXC direct-seat mode changes deployment responsibilities and does not support every backend in the same way. Operators need a clear support matrix before trying to deploy LXC mode, especially because Looking Glass and Moonlight have additional device, compositor, or guest requirements that are not part of the LXC MVP.

## Goals / Non-goals

Goals:

- Document the difference between host-direct and LXC direct-seat profiles.
- Publish the LXC MVP backend support matrix.
- State that SPICE and loopback VNC are the LXC MVP console targets.
- State that Looking Glass is excluded from LXC MVP with future reconsideration only.
- State that Moonlight is unsupported pending future feasibility work.
- Document host and container responsibility boundaries.
- Provide operator troubleshooting entry points for LXC-specific failures.

Non-goals:

- Implementing backend support.
- Writing a full package manager matrix for every distribution.
- Promising support for privileged containers.
- Adding Looking Glass or Moonlight LXC implementation plans beyond future consideration notes.

## User stories

- As an operator, I want to know which console backends are expected to work before choosing LXC mode.
- As a maintainer, I want docs that prevent accidental expansion of the LXC support promise.
- As a reviewer, I want README and setup docs to explain the security tradeoffs of direct-seat LXC.

## Public API / Interfaces

Documentation surfaces to update:

```text
README.md
docs/proxmox-host-setup.md
specs/README.md
```

Support matrix terms:

```text
supported
unsupported
future consideration
spike-only
```

Required LXC MVP matrix:

| Backend | LXC direct-seat status | Notes |
| --- | --- | --- |
| SPICE | supported after Spec 87 | Ticket creation through host proxy; `.vv` generated in container |
| VNC | supported after Spec 88 | Host loopback stays private; LXC uses managed forward/endpoint |
| Looking Glass | unsupported; future consideration | KVMFR/IVSHMEM and shared-memory device passthrough are out of MVP |
| Moonlight | unsupported pending future feasibility | Requires separate validation of Qt, sway, DRM, and streaming behavior in LXC |

The docs must also describe that `host-direct` remains the default profile and `lxc-direct-seat` is explicit.

## Data model / Persistence

No new runtime persistence is required by this documentation spec.

Documentation must describe existing or planned config fields:

```toml
[runtime]
profile = "host-direct" # or "lxc-direct-seat"
```

Docs must state that CTID belongs to host install commands and install-state, not container runtime config.

## Security model (Permission/Isolation/Audit)

Permissions:

- Docs must state that LXC direct-seat still grants the container controlled access to host physical seat resources.
- Docs must state that privileged containers are not supported.
- Docs must state that the host proxy exposes only a single-VM high-level API.

Isolation:

- Docs must distinguish host responsibilities from container responsibilities.
- Docs must warn that `tty1`, DRM, seatd socket, and power-button passthrough are appliance-level ownership decisions.

Audit:

- Docs must tell operators where install-state records host and LXC config changes.
- Docs must tell operators to inspect dry-run output before applying LXC config changes.
- Docs must list key journal units for troubleshooting host and container sides.

## Acceptance criteria (Testable, Verifiable)

- README states that host-direct remains the default deployment model.
- README introduces LXC direct-seat as an explicit profile, not the default.
- README or setup docs include the LXC support matrix with SPICE and VNC as MVP supported targets.
- Looking Glass is documented as unsupported in LXC MVP with future reconsideration only.
- Moonlight is documented as unsupported pending future feasibility.
- Docs explain that CTID is not stored in runtime config.
- Docs explain dry-run/apply behavior for host LXC config changes.
- Docs explain install-state rollback expectations for host services and LXC config.
- Docs include troubleshooting pointers for seatd socket permission, DRM selection ambiguity, tty1 ownership, and VNC forwarding.

## Test plan

Documentation verification:

- Review README and setup docs for the required support matrix.
- Search docs for misleading claims that all backends support LXC mode.
- Search docs for any suggestion to use privileged LXC as a fallback.
- Confirm `specs/README.md` lists the LXC spec series and dependency order.

Automated checks may include simple text assertions for key matrix statuses if the repository adopts documentation tests.

## Rollout / Backward compatibility

Documentation changes are additive. Existing host-direct instructions remain valid and should stay first in the quickstart unless LXC support becomes the primary deployment model later.

Operators upgrading from earlier versions must not be told that their host-direct install changed profiles automatically.

## Open questions

- Should LXC direct-seat documentation live in the existing Proxmox host setup guide or a separate LXC setup guide?
- Should unsupported backend config values fail validation in LXC mode immediately, or degrade at runtime with a documentation link?
- Should future Looking Glass reconsideration be tracked as a numbered spec placeholder or left as a non-goal note?

## Spec Dependencies

- Spec 80: LXC Direct-Seat Feasibility Spike
- Spec 81: LXC Power Button Passthrough Spike
- Spec 82: LXC Host Bootstrap Profile
- Spec 83: LXC Guest Install Profile
- Spec 85: Host VM-Control Proxy
- Spec 87: SPICE Backend for LXC
- Spec 88: VNC Backend for LXC
