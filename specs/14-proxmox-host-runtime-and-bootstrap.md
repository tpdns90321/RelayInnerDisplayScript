# Spec 14. Proxmox Host Runtime and Bootstrap

## Context / Problem

The MVP is intentionally narrow: it should work immediately on a Proxmox host without requiring a separate LXC image, GUI-based provisioning, or a packaging pipeline before the first usable pilot. That means the runtime and installation spec must define one direct-host bootstrap path that leaves the relay appliance recoverable after host reboot and understandable to an operator who administers Proxmox primarily through shell and systemd.

The runtime needs:

- a Cage kiosk stack on the host
- `remote-viewer` and a Wayland power helper
- a Python runtime with the chosen script dependencies
- local access to the host DRM and input devices
- systemd units and host policy overrides

Without a packaging spec, implementation would drift between "throw scripts in the repo" and "build a full Debian package." MVP needs a concrete middle ground.

## Goals / Non-goals

Goals:

- Define one reproducible host-direct installation flow for MVP.
- Specify required packages, system users, paths, services, and host policy overrides.
- Leave the system bootable into the relay flow after Proxmox host reboot.
- Keep the runtime aligned with the daemon/session split from earlier specs.

Non-goals:

- LXC packaging in MVP
- OCI container packaging
- Proxmox GUI installer integration
- Full Debian package publishing before the first pilot

## User stories

- As an operator, I can clone or copy the repo onto a Proxmox host, run the installer, and get a working relay appliance.
- As an operator, I can tell exactly which packages and host settings are required.
- As an operator, I can reboot the Proxmox host and the relay returns automatically.
- As a maintainer, I can evolve the scripts without forcing an immediate package build pipeline.

## Public API / Interfaces

Supported deployment target:

- Proxmox VE host direct install only for MVP

Required host packages:

- `python3`
- `python3-evdev`
- `cage`
- `seatd`
- `virt-viewer`
- `wlopm`

Managed filesystem layout:

- `/usr/local/lib/relayinner-display/` for Python entrypoints and helper scripts
- `/usr/local/share/relayinner-display/` for static assets
- `/etc/relayinner-display/config.toml` for local configuration
- `/etc/systemd/system/` for installed service units
- `/etc/systemd/logind.conf.d/relayinner-display.conf` for host power-button override

Required service units:

- `relayinner-display-seatd.service`
- `relayinner-display-kiosk.service`
- `relayinner-displayd.service`

Installer outputs:

- one idempotent installer script in the repository, `./install.sh`
- one sample config file, `./config.example.toml`
- one host setup guide, `./docs/proxmox-host-setup.md`

Installer responsibilities:

- validate the host is Proxmox VE and systemd-based
- install or verify required packages
- create the `relayinner-display` system user
- place scripts, assets, config template, and units in their target paths
- install the logind override for power-button handling
- enable required services
- print any remaining manual steps, such as editing the VMID in config

## Data model / Persistence

Persistent files on the host:

- installed scripts and assets under `/usr/local/`
- config under `/etc/relayinner-display/`
- service units under `/etc/systemd/system/`
- host policy override under `/etc/systemd/logind.conf.d/`

Mutable operator-owned files:

- `/etc/relayinner-display/config.toml`

Persistence rules:

- Re-running the installer must not overwrite an existing operator config without explicit backup or confirmation.
- Runtime state remains under `/run/relayinner-display/` and is not persisted across reboot.

## Security model (Permission/Isolation/Audit)

Permissions:

- The host install runs with root privileges.
- The kiosk session runs under a dedicated non-login user.
- Only the daemon keeps root ownership at runtime.

Isolation:

- The install does not create a general-purpose desktop user.
- Only the minimum host policy overrides required for kiosk ownership and power-button handling are applied.
- Manual operator changes outside the documented paths are out of scope for MVP support.

Audit:

- The installer logs each major step to stdout and journald where appropriate.
- Validation failures must be explicit about which package, unit, or host assumption is missing.
- The documentation must enumerate every managed path and service.

## Acceptance criteria (Testable, Verifiable)

- A fresh Proxmox host can install the MVP scripts and start all three services successfully.
- After host reboot, the services return automatically and the appliance converges to waiting or active relay state.
- Missing required packages produce clear install-time or startup-time errors.
- The setup guide is sufficient to reproduce the installation on another identical Proxmox host without undocumented steps.

## Test plan

Bootstrap tests:

- Install on a clean Proxmox host
- Re-run the installer and verify idempotent behavior
- Reboot the host after installation

Failure tests:

- Attempt install without one required package source available
- Start runtime with missing config
- Start runtime with missing power helper or viewer binary

Documentation checks:

- Follow the host setup guide from scratch on a second clean host
- Verify every documented path and unit exists after install

## Rollout / Backward compatibility

MVP rollout supports only the host-direct path. If later work adds LXC packaging or Debian packages, those must be additive paths that preserve:

- config path `/etc/relayinner-display/config.toml`
- runtime directory `/run/relayinner-display`
- service names beginning with `relayinner-display`

The MVP installer may evolve internally, but it must continue to preserve existing operator configuration unless the operator explicitly opts into a replacement.

## Open questions

- Whether `python3-evdev` is available by default in all target Proxmox releases or needs a fallback installation path.
- Whether a packaged `.deb` should be introduced immediately after MVP stabilization or only once pilots are complete.
- Whether Proxmox hosts with alternate GPU stacks need additional documented prerequisites.

## Spec Dependencies

- Spec 10. Proxmox Local Console Relay Core
- Spec 11. Cage Kiosk Session Shell
- Spec 12. VM Power-State to Host DPMS Control
- Spec 13. Host Power Button to Guest Power Control
