# Spec 17. Safe Uninstall Flow and README Removal Guide

## Context / Problem

The repository currently defines how to install the relay appliance onto a Proxmox host, but it does not yet define how to remove it safely. That leaves several operational risks:

- systemd units may remain enabled after files are removed
- the host power-button override may continue to suppress normal logind behavior
- `tty1` may remain masked away from `getty@tty1.service`
- operators may delete files manually without knowing which config or host-state changes should be preserved

Once direct-host installation exists, direct-host removal must also exist. MVP removal should be conservative, reversible where possible, and explicit about what is preserved by default versus what is purged only on request.

## Goals / Non-goals

Goals:

- Define one root-owned uninstall entrypoint for the host-direct deployment path.
- Remove relay-managed services, assets, and host policy overrides safely.
- Restore `tty1` ownership to the normal host login path.
- Preserve operator config by default while supporting an explicit purge mode.
- Add a short removal path to `README.md` and a detailed removal section to `docs/proxmox-host-setup.md`.

Non-goals:

- Removing packages installed through `apt-get`
- Rolling back unrelated host changes outside the relay-managed paths
- Restoring arbitrary third-party display manager state without prior install-state evidence
- Supporting uninstall for non-host deployment targets such as LXC or future packaging systems

## User stories

- As an operator, I can run one uninstall command and stop the relay appliance cleanly.
- As an operator, I can preserve my config by default in case I want to reinstall later.
- As an operator, I can choose a full purge when I want the relay configuration removed too.
- As a maintainer, I can trust uninstall to reverse only the host changes that the installer actually made.

## Public API / Interfaces

Uninstall entrypoint:

```sh
sudo ./uninstall.sh
```

Uninstall flags:

- `./uninstall.sh --purge-config`

Wrapper behavior:

- `uninstall.sh` is a root-checking shell wrapper parallel to `install.sh`.
- It must delegate into the Python bootstrap layer instead of duplicating uninstall logic in shell.

Default uninstall contract:

- stop and disable:
  - `relayinner-display-seatd.service`
  - `relayinner-display-kiosk.service`
  - `relayinner-displayd.service`
- remove:
  - `/usr/local/lib/relayinner-display/`
  - `/usr/local/share/relayinner-display/`
  - `/etc/systemd/system/relayinner-display-seatd.service`
  - `/etc/systemd/system/relayinner-display-kiosk.service`
  - `/etc/systemd/system/relayinner-displayd.service`
  - `/etc/systemd/logind.conf.d/relayinner-display.conf`
- run `systemctl daemon-reload`
- restore `getty@tty1.service`
- preserve `/etc/relayinner-display/config.toml`
- remove `/var/lib/relayinner-display/install-state.json` at the end of a successful uninstall

Purge contract:

- `--purge-config` performs the default uninstall and also removes:
  - `/etc/relayinner-display/config.toml`
  - `/etc/relayinner-display/config.toml.bak.*`
  - `/etc/relayinner-display/` if it becomes empty after purge

Install-state contract used by uninstall:

- Uninstall consumes `/var/lib/relayinner-display/install-state.json` when available.
- The state file defined in Spec 16 is the canonical restore source for:
  - whether the service user was created by the installer
  - whether `display-manager.service` existed and was changed
  - the canonical managed paths to remove

Fallback behavior when install-state is missing:

- Uninstall must still remove the canonical relay units, runtime trees, and logind override by known path.
- Uninstall must still restore `getty@tty1.service`.
- Uninstall must not attempt to restore `display-manager.service` without install-state evidence that the installer changed it.
- Uninstall must not remove the `relayinner-display` system user when install-state is missing, because authorship of that user cannot be proven safely.
- Uninstall must emit a clear warning that it ran in best-effort mode.

Service-user restore rules:

- If `service_user.created_by_installer=true`, uninstall removes the `relayinner-display` user and `/var/lib/relayinner-display/` after stopping services.
- If `service_user.created_by_installer=false`, uninstall leaves the user and home directory intact, but still removes `install-state.json`.

Conflicting-unit restore rules:

- `getty@tty1.service` must be unmasked and started again on successful uninstall.
- If install-state says `getty@tty1.service.enabled_before=true`, uninstall must also enable it persistently.
- If install-state says `getty@tty1.service.enabled_before=false`, uninstall must start it for the current boot and leave it disabled for future boots.
- `display-manager.service` is restored only when:
  - `existed=true`
  - `changed_by_installer=true`
- When restoring `display-manager.service`:
  - unmask it only if `masked_before=false`
  - re-enable it only if `enabled_before=true`
  - start it immediately only if `active_before=true`

README and docs contract:

- `README.md` must include a short `Uninstall` section.
- That section must document:
  - `sudo ./uninstall.sh`
  - config preservation as the default
  - `--purge-config` for full cleanup
  - link to the detailed host setup guide
- `docs/proxmox-host-setup.md` must add a detailed removal section with the service, path, and recovery expectations defined above.

## Data model / Persistence

Persistent data involved in uninstall:

- `/var/lib/relayinner-display/install-state.json`
- `/etc/relayinner-display/config.toml`
- `/etc/relayinner-display/config.toml.bak.*`

State transition rules:

- A successful default uninstall deletes install-state after all restore steps complete.
- A successful purge deletes install-state and config artifacts after all restore steps complete.
- If uninstall fails mid-run, it must leave a clear error and keep any remaining install-state file so the next run can continue from authoritative state.

Best-effort removal ordering:

1. stop relay services
2. disable relay services
3. remove relay unit files and logind override
4. run `systemctl daemon-reload`
5. restore `getty@tty1.service`
6. restore `display-manager.service` only if install-state authorizes it
7. remove runtime trees and shared assets
8. remove service user and home only if install-state authorizes it
9. remove config only in purge mode
10. delete install-state last

## Security model (Permission/Isolation/Audit)

Permissions:

- Uninstall runs as root.
- Removal is limited to canonical relay-managed paths or paths explicitly recorded in install-state.
- Purge mode must only affect relay config files and their relay-created backups.

Isolation:

- Uninstall must not delete arbitrary `/usr/local/`, `/etc/systemd/`, or `/etc/` content beyond the documented relay-managed paths.
- The fallback path without install-state must prefer leaving possible leftovers over removing ambiguous host state.

Audit:

- Uninstall must print each major removal or restore step to stdout.
- Best-effort mode without install-state must emit one warning that restore precision is reduced.
- Purge mode must explicitly report that config files were deleted.

## Acceptance criteria (Testable, Verifiable)

- `sudo ./uninstall.sh` stops and disables all relay services and removes their installed runtime files.
- Default uninstall preserves `/etc/relayinner-display/config.toml`.
- `sudo ./uninstall.sh --purge-config` removes the config file and relay-created backups.
- Successful uninstall restores `getty@tty1.service` so the host has a normal local login path again.
- `display-manager.service` is restored only when install-state proves the installer changed it.
- If install-state is missing, uninstall still cleans the known relay assets and warns that it used best-effort mode.
- If the relay user was not created by the installer, uninstall leaves that user intact.

## Test plan

Functional tests:

- Install on a clean host, then run `sudo ./uninstall.sh` and verify services, unit files, runtime trees, and logind override are gone.
- Install on a clean host, then run `sudo ./uninstall.sh --purge-config` and verify config plus backups are gone.
- Reinstall after uninstall and verify the installer succeeds without leftover conflicting state.

Restore-state tests:

- Install on a host with no `display-manager.service` and verify uninstall does not attempt to restore it.
- Install on a host with an enabled, running `display-manager.service` and verify uninstall restores it only if install-state recorded installer changes.
- Verify `getty@tty1.service` becomes usable again after uninstall.

Failure and safety tests:

- Delete `install-state.json` manually, run uninstall, and verify best-effort cleanup plus warning behavior.
- Delete one relay-managed file manually before uninstall and verify the uninstall continues without crashing.
- Verify default uninstall leaves `/etc/relayinner-display/config.toml` untouched.

## Rollout / Backward compatibility

This spec adds an uninstall path to the existing host-direct install model without changing the runtime contract. It preserves:

- the same relay service names
- the same config path
- the same managed runtime directory layout introduced by install

Future package-based deployment may wrap uninstall differently, but it must preserve the default semantics defined here:

- safe removal of runtime assets
- default config preservation
- explicit purge-only config deletion
- conservative restoration of host services based on recorded install-state

## Open questions

- Whether uninstall should support a `--dry-run` mode once the basic path is implemented.
- Whether config backups should remain after purge when they were not created by the relay installer.
- Whether reinstall should warn when it detects a preserved config from a previous uninstall.

## Spec Dependencies

- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 16. Proxmox Host Installation Flow and README Quickstart
