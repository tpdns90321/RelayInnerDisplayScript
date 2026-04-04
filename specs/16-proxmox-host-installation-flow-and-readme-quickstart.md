# Spec 16. Proxmox Host Installation Flow and README Quickstart

## Context / Problem

Spec 14 defines the direct-host bootstrap contract and the repository now ships a working `install.sh`, sample config, and host setup guide. That is enough for a maintainer who already understands the codebase, but it still leaves two operator-facing gaps:

- the root `README.md` does not yet act as a first-run installation entrypoint
- the installer does not persist a structured record of which host mutations it applied

Those gaps become operationally important as soon as the project needs a documented install path that can also support later removal or upgrade work. The repository needs one installation-onboarding spec that turns the existing bootstrap flow into a clear operator contract and records the host changes required to reverse that install safely.

## Goals / Non-goals

Goals:

- Define the operator-facing installation flow that starts in `README.md`.
- Keep `README.md`, `docs/proxmox-host-setup.md`, and `install.sh` aligned as one documented contract.
- Specify a persistent install-state record that captures the host mutations performed by the installer.
- Preserve the current host-direct install model and the existing `install.sh` command shape.

Non-goals:

- Adding a new deployment target such as `.deb`, OCI, or LXC packaging
- Replacing the existing Python bootstrap implementation
- Defining upgrade or migration semantics beyond recording install-state
- Redesigning the daemon, kiosk, or session runtime contracts from earlier specs

## User stories

- As an operator, I can open `README.md` and find the shortest correct path to install the appliance.
- As an operator, I can tell which settings must be edited before the relay is usable.
- As a maintainer, I can compare the README, setup guide, and installer flags and see one coherent install contract.
- As a maintainer, I can later implement uninstall behavior without guessing which host state the installer changed.

## Public API / Interfaces

Supported deployment target:

- Proxmox VE host direct install only

Installer entrypoint:

```sh
sudo ./install.sh
```

Installer flags carried forward from the current bootstrap contract:

- `./install.sh --skip-package-install`
- `./install.sh --replace-config`

README contract:

- The root `README.md` must include an `Install` or `Quickstart` section.
- That section must document, in this order:
  - supported host assumptions
  - repository copy or clone step
  - `sudo ./install.sh`
  - required edits in `/etc/relayinner-display/config.toml`
  - first restart or reboot verification
  - link to `./docs/proxmox-host-setup.md` for the full operator guide

Detailed setup guide contract:

- `./docs/proxmox-host-setup.md` remains the single detailed operator procedure.
- It must document:
  - required packages
  - installer flags
  - managed paths
  - managed services
  - post-install verification commands
  - troubleshooting entrypoints

Install-state contract:

- Successful install writes `/var/lib/relayinner-display/install-state.json`.
- The file is root-owned and stored with mode `0640`.
- The file must be rewritten on every successful installer run so it reflects the latest known host state.

Required install-state schema:

```json
{
  "schema_version": 1,
  "installed_at": "2026-04-04T12:00:00Z",
  "managed_paths": {
    "lib_dir": "/usr/local/lib/relayinner-display",
    "share_dir": "/usr/local/share/relayinner-display",
    "config_dir": "/etc/relayinner-display",
    "config_path": "/etc/relayinner-display/config.toml",
    "logind_override_path": "/etc/systemd/logind.conf.d/relayinner-display.conf",
    "service_home": "/var/lib/relayinner-display",
    "systemd_units": [
      "/etc/systemd/system/relayinner-display-seatd.service",
      "/etc/systemd/system/relayinner-display-kiosk.service",
      "/etc/systemd/system/relayinner-displayd.service"
    ]
  },
  "config_state": {
    "action": "created",
    "backup_path": null
  },
  "service_user": {
    "name": "relayinner-display",
    "created_by_installer": true
  },
  "conflicting_units": {
    "getty@tty1.service": {
      "existed": true,
      "enabled_before": true,
      "active_before": true,
      "masked_before": false,
      "changed_by_installer": true
    },
    "display-manager.service": {
      "existed": false,
      "enabled_before": false,
      "active_before": false,
      "masked_before": false,
      "changed_by_installer": false
    }
  }
}
```

Install-state rules:

- `config_state.action` must be one of `created`, `preserved`, or `replaced`.
- `config_state.backup_path` must contain the exact backup path when `--replace-config` replaced an existing config, otherwise `null`.
- `service_user.created_by_installer` is `true` only when this installer invocation created the system user.
- `conflicting_units` must record the pre-install state before the installer disables or masks those units.
- `display-manager.service` must be recorded only if the unit name exists on the host; if it is absent, `existed=false` and `changed_by_installer=false`.

## Data model / Persistence

Persistent files created or managed by install:

- `/usr/local/lib/relayinner-display/`
- `/usr/local/share/relayinner-display/`
- `/etc/relayinner-display/config.toml`
- `/etc/systemd/system/relayinner-display-seatd.service`
- `/etc/systemd/system/relayinner-display-kiosk.service`
- `/etc/systemd/system/relayinner-displayd.service`
- `/etc/systemd/logind.conf.d/relayinner-display.conf`
- `/var/lib/relayinner-display/install-state.json`

Persistence rules:

- The install-state file is the authoritative source for later uninstall restore behavior.
- The file records installer-managed host state only; it must not store secrets or VM credentials.
- Re-running install without `--replace-config` preserves operator config and updates `config_state.action` to `preserved`.
- Re-running install with `--replace-config` must update `config_state.action` to `replaced` and capture the backup path that was created during that run.

## Security model (Permission/Isolation/Audit)

Permissions:

- The installer runs as root.
- The install-state file is writable only by root.
- The README and setup guide must continue to describe the kiosk runtime as a dedicated non-login user model.

Isolation:

- The documented install path must not introduce a general-purpose desktop session.
- The install-state file must only describe known managed paths and unit names; it must not support arbitrary path injection.

Audit:

- Installer stdout remains the primary operator-facing log for install steps.
- Install-state creation is an auditable record of what the installer changed on the host.
- The README and detailed guide must enumerate every installer-managed path and service without undocumented side effects.

## Acceptance criteria (Testable, Verifiable)

- `README.md` contains a short installation section that points operators to the existing install command and detailed setup guide.
- `README.md`, `docs/proxmox-host-setup.md`, and the installer flags exposed by `install.sh` describe the same install flow.
- A successful install writes `install-state.json` with the required schema and current host facts.
- Re-running the installer updates the install-state file without losing the current config preservation or replacement outcome.
- The recorded unit state is sufficient to decide later whether `display-manager.service` should be restored.

## Test plan

Documentation checks:

- Read only `README.md` and verify the install entrypoint, required config edits, and first verification steps are discoverable.
- Compare `README.md` and `docs/proxmox-host-setup.md` and verify the package list, flags, service names, and key paths match.

Installer-state checks:

- Run install on a clean host and verify `install-state.json` is created with `config_state.action=created`.
- Re-run install without `--replace-config` and verify `config_state.action=preserved`.
- Re-run install with `--replace-config` and verify `config_state.action=replaced` plus a non-null backup path.
- Install on a host with `display-manager.service` absent and verify the file records `existed=false`.
- Install on a host with `display-manager.service` present and verify the file captures whether the installer changed it.

Operational checks:

- Verify the install-state file contains only canonical relay-managed paths.
- Verify the file remains readable to root and not world-writable.

## Rollout / Backward compatibility

This spec is additive to the existing host bootstrap path. It preserves:

- `./install.sh` as the operator entrypoint
- `/etc/relayinner-display/config.toml` as the canonical config path
- `/run/relayinner-display/` as the runtime state directory
- service names beginning with `relayinner-display`

Future packaging work may change the implementation details behind install, but it must either continue to write the same install-state schema or provide a compatibility layer that preserves uninstall behavior defined by later specs.

## Open questions

- Whether install-state should also record the exact installer version string or git commit for troubleshooting.
- Whether `README.md` should include a short verification snippet inline or only link to the detailed guide.
- Whether future upgrade work should reuse `install-state.json` directly or introduce a separate upgrade-state contract.

## Spec Dependencies

- Spec 14. Proxmox Host Runtime and Bootstrap
- Spec 15. MVP Integration, Failure Policy, and Ops
