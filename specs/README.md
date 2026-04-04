# RelayInnerDisplayScript Specs

This directory contains the MVP spec set for a Proxmox-hosted display relay appliance that mirrors one KVM guest directly onto a host-attached monitor.

## Spec Index

- `10-proxmox-local-console-relay-core.md`
- `11-cage-kiosk-session-shell.md`
- `12-vm-power-state-to-host-dpms-control.md`
- `13-host-power-button-to-guest-power-control.md`
- `14-proxmox-host-runtime-and-bootstrap.md`
- `15-mvp-integration-failure-policy-and-ops.md`
- `16-proxmox-host-installation-flow-and-readme-quickstart.md`
- `17-safe-uninstall-flow-and-readme-removal-guide.md`

## Product Summary

RelayInnerDisplayScript turns a Proxmox host with an attached monitor into a single-purpose guest display relay:

- It boots directly into a Cage kiosk session.
- It shows one target VM on the attached display using SPICE and `remote-viewer`.
- It wakes or sleeps the host monitor based on the VM power state.
- It forwards the physical host power button to guest start or shutdown behavior.
- It installs directly on the Proxmox host for the MVP rather than inside an LXC container.

## Shared Defaults

- Deployment target: Proxmox host direct install
- Runtime model: Python scripts plus systemd units
- Console backend: SPICE via `remote-viewer`
- Proxmox control path: local `qm` and `pvesh`
- Display policy: monitor on when VM is active, monitor standby when VM is off
- Power button policy: start when VM is off, graceful shutdown when VM is on

## Dependency Order

1. Spec 10
2. Spec 11
3. Spec 12 and Spec 13
4. Spec 14
5. Spec 15
6. Spec 16
7. Spec 17
