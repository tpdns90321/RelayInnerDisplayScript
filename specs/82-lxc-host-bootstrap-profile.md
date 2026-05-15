# Spec 82. LXC Host Bootstrap Profile

## Context / Problem

The LXC direct-seat deployment requires host-owned preparation before the unprivileged container can run the relay kiosk. The host must coordinate `tty1`, `seatd`, logind, LXC raw config, device passthrough, and rollback state. This is more dangerous than copying application files into a container, so it must be an explicit install profile with dry-run behavior and strong preflight checks.

## Goals / Non-goals

Goals:

- Add a host-side LXC direct-seat bootstrap profile.
- Keep dry-run as the default behavior.
- Require an explicit apply flag before modifying `/etc/pve/lxc/<ctid>.conf`.
- Refuse unsafe conditions before writing.
- Record enough install-state to restore the host and LXC config.
- Use a managed block plus full-file backup for Proxmox LXC config changes.

Non-goals:

- Installing packages inside the container.
- Running the relay daemon inside the container.
- Implementing host VM-control proxy behavior.
- Supporting privileged containers.
- Supporting broad device class passthrough.

## User stories

- As an operator, I want to preview exactly how my CT config will change before any write happens.
- As a maintainer, I want all host-side LXC changes recorded for safe uninstall.
- As a reviewer, I want apply mode to stop when the CT is running or when existing config conflicts with the managed block.

## Public API / Interfaces

New host install profile:

```sh
sudo ./install.sh --profile lxc-direct-seat-host --ctid <ctid>
sudo ./install.sh --profile lxc-direct-seat-host --ctid <ctid> --apply-lxc-config
```

Default behavior without `--apply-lxc-config` is dry-run. Dry-run must print:

- CTID
- CT config path
- selected DRM devices
- selected tty device
- selected power-button device when enabled by the relevant spec
- generated `lxc.cgroup2.devices.allow` lines
- generated `lxc.mount.entry` lines
- proposed idmap changes
- host service changes
- rollback plan

Managed block shape:

```text
# BEGIN relayinner-display lxc-direct-seat
...
# END relayinner-display lxc-direct-seat
```

## Data model / Persistence

Install-state must be extended with an LXC host profile record:

```json
{
  "profile": "lxc-direct-seat-host",
  "lxc": {
    "ctid": "100",
    "config_path": "/etc/pve/lxc/100.conf",
    "backup_path": "/var/lib/relayinner-display/backups/lxc-100.conf.<timestamp>",
    "managed_block": "...",
    "devices": [
      {"path": "/dev/dri/card0", "major": 226, "minor": 0, "access": "rwm"}
    ],
    "gid_mappings": []
  }
}
```

The existing install-state schema version must be advanced when this structure is added. Uninstall must tolerate older install-state files that do not contain the LXC section.

The full original LXC config must be backed up before any managed block write. The backup path must be outside `/etc/pve` and owned by the relay install-state lifecycle.

## Security model (Permission/Isolation/Audit)

Permissions:

- Apply mode requires root on the Proxmox host.
- Apply mode only targets the CTID supplied on the command line.
- Device rules are rendered from concrete device major/minor values.

Isolation:

- The profile refuses privileged containers.
- The profile refuses to apply to a running CT.
- The profile refuses broad character-device class rules.
- The profile refuses unmanaged duplicate relay blocks.

Audit:

- Dry-run output is the primary operator review surface.
- Apply output must state the backup path and every written file.
- Install-state must preserve enough data for uninstall and manual rollback.

## Acceptance criteria (Testable, Verifiable)

- Running the profile without `--apply-lxc-config` does not modify any host file.
- Apply mode refuses when CTID is missing.
- Apply mode refuses when `/etc/pve/lxc/<ctid>.conf` does not exist.
- Apply mode refuses when the target CT is running.
- Apply mode refuses when the target CT is privileged.
- Apply mode creates a full config backup before writing.
- Apply mode inserts or updates only one relay managed block.
- Apply mode records CTID, config path, backup path, devices, idmap changes, and managed block in install-state.
- Uninstall restores the previous LXC config or removes the managed block according to install-state.
- Host-direct install behavior remains unchanged when no LXC profile is selected.

## Test plan

Automated tests should use staged filesystem fixtures for:

- dry-run no-op behavior
- CT config missing
- running CT refusal through a fake `pct status`
- privileged CT refusal
- duplicate managed block detection
- full backup creation
- managed block replacement
- install-state serialization and loading
- uninstall restoration

Manual verification on Proxmox:

1. Stop a Debian 12 unprivileged CT.
2. Run dry-run and inspect output.
3. Run apply mode.
4. Inspect `/etc/pve/lxc/<ctid>.conf` for one managed block.
5. Inspect install-state.
6. Run uninstall and confirm config restoration.

## Rollout / Backward compatibility

The profile is opt-in. Existing `sudo ./install.sh` remains the host-direct path until a later spec explicitly changes defaults.

Install-state loading must remain backward compatible with host-direct installs created before this profile existed.

## Open questions

- Should backup files live under `/var/lib/relayinner-display/backups` or beside the existing install-state file?
- Should dry-run support JSON output for automated review?
- Should applying a changed managed block require an additional `--replace-existing-lxc-block` flag?

## Spec Dependencies

- Spec 14: Proxmox Host Runtime and Bootstrap
- Spec 16: Proxmox Host Installation Flow and README Quickstart
- Spec 17: Safe Uninstall Flow and README Removal Guide
- Spec 80: LXC Direct-Seat Feasibility Spike
- Spec 81: LXC Power Button Passthrough Spike
