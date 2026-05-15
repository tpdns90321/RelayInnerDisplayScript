# Spec 80. LXC Direct-Seat Feasibility Spike

## Context / Problem

The current relay appliance installs directly on the Proxmox host and owns the physical display path from the host. Operators want a safer deployment shape where the kiosk compositor and relay runtime can run inside a Debian 12 unprivileged Proxmox LXC while still driving the host-attached monitor and `tty1`.

This is not a normal LXC workload. wlroots compositors need coordinated access to DRM/KMS devices, a virtual terminal, and seat mediation. The first step must prove that a minimally exposed unprivileged container can start a compositor on the host monitor before any VM console or product integration work depends on that path.

## Goals / Non-goals

Goals:

- Prove or disprove that Debian 12 unprivileged LXC can run the relay-selected compositor on the host-attached monitor.
- Keep the first experiment limited to a fullscreen smoke application, not a VM console backend.
- Use host `seatd` through `/run/seatd.sock` as the seat mediator.
- Start from the smallest practical device and observation surface.
- Record exact failures and required additional mounts if the minimal surface is insufficient.

Non-goals:

- Implementing the full LXC install profile.
- Supporting power-button input.
- Connecting to a VM console.
- Adding a host VM-control proxy.
- Supporting Looking Glass or Moonlight in LXC mode.
- Supporting privileged containers.

## User stories

- As an operator, I want to know whether the relay kiosk can run from an unprivileged LXC before I accept a new deployment profile.
- As a maintainer, I want a bounded hardware spike that identifies the minimum device exposure needed for wlroots inside LXC.
- As a reviewer, I want failure logs that distinguish seatd, tty, DRM, udev, and sysfs blockers.

## Public API / Interfaces

The spike uses a host-side command path that prepares an existing stopped CT for a temporary experiment. The eventual implementation may reuse this shape, but this spec only requires a spike interface:

```sh
sudo ./install.sh --profile lxc-direct-seat-host --ctid <ctid> --print-lxc-config
sudo ./install.sh --profile lxc-direct-seat-host --ctid <ctid> --apply-lxc-config
```

The container-side smoke command must be explicit and reproducible. Examples:

```sh
sudo ./install.sh --profile lxc-guest --spike-only
systemctl start relayinner-display-kiosk.service
```

The LXC host profile must attempt to expose only:

```text
/run/seatd.sock
/dev/dri/cardX
/dev/dri/renderDX
/dev/tty1
/sys/class/drm  (read-only)
```

The compositor environment must include:

```text
LIBSEAT_BACKEND=seatd
```

If the bind-mounted socket is not at the default path inside the container, the environment must also include `SEATD_SOCK`.

## Data model / Persistence

Temporary spike state may be recorded in the existing install-state structure only if the host profile applies changes. The state must include:

- selected CTID
- selected DRM card path
- selected render node path
- selected tty path
- LXC config backup path
- managed LXC config block content
- host service changes made for `tty1`, display-manager conflicts, and `seatd`

The spike must not add permanent runtime state to the relay daemon. Any smoke-test logs belong in journald or an explicit operator-provided log path.

## Security model (Permission/Isolation/Audit)

Permissions:

- The container remains unprivileged.
- Device access is granted only through concrete device nodes and concrete cgroup rules.
- The host owns seat mediation through `seatd`.
- Host `getty@tty1.service` may be disabled or masked only under install-state tracking.

Isolation:

- The spike must not mount all of `/dev`, all of `/sys`, all of `/run/udev`, or input devices.
- The spike must not map broad character-device classes such as `c 4:*`, `c 13:*`, or `c 226:*` unless a later spec explicitly changes the policy.
- The spike must not use privileged LXC fallback.

Audit:

- The selected devices and their major/minor numbers must be printed before apply.
- The generated LXC lines must be visible in dry-run output.
- Any expansion beyond `/sys/class/drm` must be justified by captured compositor logs.

## Acceptance criteria (Testable, Verifiable)

- A Debian 12 unprivileged LXC can be prepared from a stopped CT without converting it to privileged mode.
- Dry-run prints the exact LXC config lines without modifying `/etc/pve/lxc/<ctid>.conf`.
- Apply mode refuses to run when the CT is running.
- Apply mode backs up the original LXC config before writing a managed block.
- The host bootstrap identifies exactly one connected DRM card or stops and asks for an override.
- The generated cgroup rules use concrete major/minor values for the selected DRM card, render node, and `/dev/tty1`.
- The container can start `cage` or `sway` on the host monitor and show a fullscreen smoke application.
- If startup fails, the failure report identifies the first failing subsystem and whether additional sysfs or udev observation paths appear necessary.

## Test plan

Manual hardware validation:

1. Create or choose a Debian 12 unprivileged Proxmox LXC.
2. Stop the CT.
3. Run the host profile in dry-run mode and review generated config.
4. Run apply mode.
5. Start the CT.
6. Install the LXC guest spike profile.
7. Start the kiosk smoke service.
8. Confirm the attached monitor shows the fullscreen smoke app on `tty1`.
9. Review host and container journals for seatd, tty, DRM, and compositor errors.

Automated unit tests should cover:

- connected DRM card discovery with zero, one, and multiple candidates
- concrete major/minor extraction
- LXC config block rendering
- refusal to apply while CT is running
- backup path recording
- dry-run output without file mutation

## Rollout / Backward compatibility

This spec is a spike and must not change the default host-direct install path. Existing `install.sh` behavior without an LXC profile remains unchanged.

The spike may introduce hidden or explicitly gated profile flags, but those flags must not be presented as production support until later implementation specs pass.

## Open questions

- Does wlroots inside an unprivileged LXC need read-only `/run/udev/data` or `/sys/dev/char` in addition to `/sys/class/drm`?
- Does the container systemd `TTYPath=/dev/tty1` model work reliably with a bind-mounted host tty?
- Is `cage` or `sway` the better first smoke compositor for this environment?
- Which Proxmox versions expose LXC config behavior needed by the managed block consistently?

## Spec Dependencies

- Spec 11: Cage Kiosk Session Shell
- Spec 14: Proxmox Host Runtime and Bootstrap
- Spec 17: Safe Uninstall Flow and README Removal Guide
- Spec 50: Kiosk Compositor Selection Contract
- Spec 51: Managed Sway Kiosk Runtime
