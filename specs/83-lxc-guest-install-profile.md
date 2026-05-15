# Spec 83. LXC Guest Install Profile

## Context / Problem

The host-direct installer currently lays down host services, a host `seatd` unit, relay runtime files, config, and kiosk services in one environment. In the LXC direct-seat deployment, those responsibilities split: the host prepares the container and physical seat, while the relay runtime files and container systemd units are installed inside the Debian 12 LXC.

The container must not install or run its own `seatd` service because seat mediation is provided by the host through a bind-mounted socket.

## Goals / Non-goals

Goals:

- Add an explicit LXC guest install profile that runs inside the container.
- Install relay runtime files and config inside the LXC.
- Render only the daemon and kiosk services inside the LXC.
- Reuse the existing `relayinner-display` runtime user and group inside the container.
- Configure the kiosk service for host `seatd` access.
- Keep host-direct install behavior unchanged.

Non-goals:

- Modifying `/etc/pve/lxc/<ctid>.conf` from inside the container.
- Installing host `seatd` or host logind overrides.
- Creating Proxmox host groups or idmap entries.
- Supporting non-Debian guest distributions in the MVP.
- Running the relay services as root.

## User stories

- As an operator, I want a clear command to run inside the LXC after the host has prepared the container.
- As a maintainer, I want container services to reflect the LXC responsibility boundary rather than carrying a disabled host-only seatd unit.
- As a reviewer, I want the LXC guest profile to fail early when host-provided socket and device paths are missing.

## Public API / Interfaces

Container-side install profile:

```sh
sudo ./install.sh --profile lxc-guest
```

The profile installs these services only:

```text
relayinner-displayd.service
relayinner-display-kiosk.service
```

The profile must not install:

```text
relayinner-display-seatd.service
```

Expected kiosk unit properties include:

```ini
Environment=LIBSEAT_BACKEND=seatd
TTYPath=/dev/tty1
StandardInput=tty
```

The config surface gains a runtime profile field:

```toml
[runtime]
profile = "lxc-direct-seat"
```

Container preflight must check for at least:

```text
/run/seatd.sock
/dev/tty1
/dev/dri/cardX or configured equivalent
```

## Data model / Persistence

Container install-state records:

- profile: `lxc-guest`
- installed service unit paths
- service user/group creation state
- config action and backup path
- expected host-provided resource paths

The container install-state must not store CTID. CTID belongs to host bootstrap state and operator commands.

The runtime config stores profile and container-relevant paths, not host install targets:

```toml
[runtime]
profile = "lxc-direct-seat"

[lxc]
host_proxy_socket = "/run/relayinner-display-host-proxy/proxy.sock"
```

Exact socket paths may be finalized by later proxy specs.

## Security model (Permission/Isolation/Audit)

Permissions:

- Runtime services run as the `relayinner-display` user/group inside the container.
- The container profile does not grant itself host device permissions.
- The profile relies on host-prepared bind mounts and idmap policy.

Isolation:

- No local `seatd` service is installed in the container.
- No privileged container assumptions are made.
- The profile does not call `pct`, edit `/etc/pve`, or run host-only commands.

Audit:

- Install output must state that the profile is container-side only.
- Install output must list missing host-provided resources.
- Install-state must identify the profile so uninstall does not try to restore host resources from inside the container.

## Acceptance criteria (Testable, Verifiable)

- `sudo ./install.sh --profile lxc-guest` installs daemon and kiosk units inside the container.
- The guest profile does not write `relayinner-display-seatd.service`.
- The guest profile creates or reuses the `relayinner-display` user and group.
- The kiosk unit includes `LIBSEAT_BACKEND=seatd`.
- The kiosk unit keeps `TTYPath=/dev/tty1` for the MVP spike path.
- The profile warns or fails when `/run/seatd.sock` is missing.
- The profile warns or fails when `/dev/tty1` is missing.
- The generated config uses `[runtime].profile = "lxc-direct-seat"`.
- Uninstall inside the container removes only container-installed assets and does not modify host LXC config.

## Test plan

Automated tests should cover:

- profile selection parsing
- guest service unit list rendering
- absence of seatd service in guest profile
- install-state profile serialization
- missing resource preflight messages
- uninstall behavior for guest profile
- host-direct behavior unchanged

Manual validation:

1. Prepare the host using the LXC host profile.
2. Start the CT.
3. Run the guest install profile inside the CT.
4. Inspect installed systemd units.
5. Start daemon and kiosk services.
6. Confirm the kiosk can reach host `seatd` and `/dev/tty1`.

## Rollout / Backward compatibility

The guest profile is opt-in and does not replace host-direct install. Existing host-direct operators continue using `sudo ./install.sh` without profile flags.

The profile should share as much runtime file installation logic as possible with host-direct to avoid drift, but service rendering must be profile-aware.

## Open questions

- Should package checks inside the guest profile be hard failures or warnings when packages are absent?
- Should the guest profile support a smoke-only mode before full daemon installation?
- Should `TTYPath=/dev/tty1` remain after the spike if libseat alone can acquire the VT without systemd TTY binding?

## Spec Dependencies

- Spec 14: Proxmox Host Runtime and Bootstrap
- Spec 16: Proxmox Host Installation Flow and README Quickstart
- Spec 17: Safe Uninstall Flow and README Removal Guide
- Spec 80: LXC Direct-Seat Feasibility Spike
- Spec 82: LXC Host Bootstrap Profile
